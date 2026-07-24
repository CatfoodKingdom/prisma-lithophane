/** Install features/filaments/index commands. */
export function installFeaturesFilamentsIndex(app) {
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
    const roles = app.commands._filamentSpecialRoles(fil);
    return roles.length ? roles.join(", ") : "None";
  }

  function _filamentPolicyHtml(fil) {
    const excluded = !!fil.exclude_from_model;
    return `
      <div class="filament-policy-grid">
        <span class="sidebar-label">White Cap</span>
        <span class="drawer-form-value">${fil.white_cap_eligible ? "Eligible" : "Not eligible"}</span>
        <span class="sidebar-label">Special Roles</span>
        <span class="drawer-form-value">${app.commands.escapeHtml(app.commands._filamentRolesLabel(fil))}</span>
        <span class="sidebar-label">Model Fit</span>
        <span class="drawer-form-value">${excluded ? "Excluded" : "Included"}</span>
        <span class="sidebar-label">Generation</span>
        <span class="drawer-form-value">${excluded ? "Unavailable for new generation" : "Available"}</span>
      </div>
    `;
  }

  function _filamentOptionsHtml(fil = {}) {
    const roles = new Set(app.commands._filamentSpecialRoles(fil));
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
    if (document.getElementById("filSpecialRoleTransparent")?.checked)
      roles.push("transparent");
    if (document.getElementById("filSpecialRoleBlack")?.checked)
      roles.push("black");
    return {
      material:
        (document.getElementById("filEditMaterial")?.value || "").trim() ||
        "unknown",
      whiteCapEligible: !!document.getElementById("filWhiteCapEligible")
        ?.checked,
      specialRoles: roles,
      excludeFromModel: !!document.getElementById("filExcludeFromModel")
        ?.checked,
      notes: document.getElementById("filEditNotes")?.value || "",
    };
  }

  function _setFilamentValidation(validation, message, fields = []) {
    document.querySelectorAll(".filament-field-invalid").forEach((el) => {
      el.classList.remove("filament-field-invalid");
    });
    if (!validation) return;
    validation.textContent = message || "";
    validation.className = message
      ? "filament-validation is-error"
      : "filament-validation";
    fields.forEach((field) => field?.classList?.add("filament-field-invalid"));
    if (message) app.commands.showImportToast(message, "error");
  }

  function renderSidebarForFilament(fil) {
    app.state.filaments._filamentDrawerMode = "view";
    app.state.filaments._filamentDrawerData = fil;
    app.dom.recordDrawer.classList.remove("sample-expanded");
    app.dom.recordDrawer.classList.remove("sample-set-drawer");
    app.dom.recordDrawer.classList.remove("model-filament-drawer");
    app.dom.recordDrawer.classList.add("narrow-drawer");
    app.commands._renderFilamentDrawerView(fil);
    app.commands.openRecordDrawer();
  }

  function openFilamentCreateDrawer() {
    app.state.filaments._filamentDrawerMode = "create";
    app.state.filaments._filamentDrawerData = null;
    app.state.logbook.selectedRecord = { kind: "filament", id: "__new__" };
    app.dom.recordDrawer.classList.remove("sample-expanded");
    app.dom.recordDrawer.classList.remove("sample-set-drawer");
    app.dom.recordDrawer.classList.remove("model-filament-drawer");
    app.dom.recordDrawer.classList.add("narrow-drawer");
    app.commands._renderFilamentDrawerCreate();
    app.commands.openRecordDrawer();
  }

  function _renderFilamentDrawerView(fil) {
    app.commands.setDetailSidebarStackMode("form");
    app.commands.setDrawerHeading(fil.display_name || fil.filament_id);
    const status = app.commands.filamentStatusMeta(fil);
    const usedBySection = app.commands.buildFilamentUsedBySection(
      fil.filament_id,
    );
    app.dom.detailActionArea.innerHTML = `
      <button class="ghost-button small drawer-header-action" id="filDrawerEditBtn">Edit</button>
    `;
    app.dom.detailWindowArea.innerHTML = "";
    app.dom.drawerStatusPill.innerHTML = `<span class="status-pill ${status.cls}">${status.label}</span>`;

    app.dom.detailSidebar.innerHTML = `
      ${app.commands.buildDrawerFormModule(
        "Filament",
        `
        <div class="filament-header-row">
          <span class="filament-drawer-chip" style="background:${app.commands.escapeHtml(fil.hex)}"></span>
          <strong>${app.commands.escapeHtml(fil.color_name || fil.display_name || fil.filament_id)}</strong>
        </div>
      `,
        { density: "compact" },
      )}
      ${app.commands.buildDrawerFormModule("Manufacturer", `<span class="drawer-form-value">${app.commands.escapeHtml(fil.manufacturer)}</span>`, { density: "compact" })}
      ${app.commands.buildDrawerFormModule("Material", `<span class="drawer-form-value">${app.commands.escapeHtml(app.commands._filamentMaterial(fil))}</span>`, { density: "compact" })}
      ${app.commands.buildDrawerFormModule(
        "Color",
        `
        <div class="filament-color-labeled">
          <span class="sidebar-label">Hex</span>
          <span class="mono">${app.commands.escapeHtml(fil.hex)}</span>
          <span class="sidebar-label">RGB</span>
          <span class="mono">${app.commands.escapeHtml(app.commands.hexToRgbString(fil.hex))}</span>
        </div>
      `,
        { density: "compact" },
      )}
      ${app.commands.buildDrawerFormModule("Policy", app.commands._filamentPolicyHtml(fil), { density: "compact" })}
      ${app.commands.buildDrawerFormModule("Notes", `<span class="drawer-form-value">${app.commands.escapeHtml(app.commands._filamentNotes(fil) || "None")}</span>`, { density: "compact" })}
      ${app.commands.buildDrawerFormModule(`Used By ${usedBySection.count} Sample${usedBySection.count === 1 ? "" : "s"}`, usedBySection.html, { density: "table" })}
      ${app.commands.buildDrawerFormModule("ID", `<span class="mono drawer-form-value filament-slug-preview">${app.commands.escapeHtml(fil.filament_id)}</span>`, { density: "compact" })}
    `;
    app.commands.bindLinkedSampleTriggers(app.dom.detailSidebar);

    document
      .getElementById("filDrawerEditBtn")
      ?.addEventListener("click", () => {
        app.state.filaments._filamentDrawerMode = "edit";
        app.commands._renderFilamentDrawerEdit(fil);
      });
  }

  function _renderFilamentDrawerEdit(fil) {
    app.commands.setDetailSidebarStackMode("form");
    app.commands.setDrawerHeading(fil.display_name || fil.filament_id);
    const status = app.commands.filamentStatusMeta(fil);
    const usedBySection = app.commands.buildFilamentUsedBySection(
      fil.filament_id,
    );
    app.dom.drawerStatusPill.innerHTML = `<span class="status-pill ${status.cls}">${status.label}</span>`;
    app.dom.detailWindowArea.innerHTML = "";
    app.dom.detailActionArea.innerHTML = `
      <button class="primary-button small drawer-header-action" id="filDrawerSaveBtn">Save</button>
      <button class="ghost-button small drawer-header-action" id="filDrawerDiscardBtn">Discard</button>
      <button class="delete-button small drawer-header-action" id="filDrawerDeleteBtn">Delete</button>
    `;

    app.dom.detailSidebar.innerHTML = `
      ${app.commands.buildDrawerFormModule(
        "Filament",
        `
        <div class="filament-header-row filament-header-row-edit">
          <span class="filament-drawer-chip" id="filEditChip" style="background:${app.commands.escapeHtml(fil.hex)}"></span>
          <input type="text" id="filEditColorName" class="filament-inline-name-input filament-drawer-input" value="${app.commands.escapeHtml(fil.color_name)}" placeholder="e.g. Basic Blue">
        </div>
      `,
        { density: "compact" },
      )}
      ${app.commands.buildDrawerFormModule(
        "Manufacturer",
        `
        <div class="filament-edit-field filament-inline-field">
          <input type="text" id="filEditMfr" class="filament-drawer-input" value="${app.commands.escapeHtml(fil.manufacturer)}">
        </div>
      `,
        { density: "compact" },
      )}
      ${app.commands.buildDrawerFormModule(
        "Material",
        `
        <div class="filament-edit-field filament-inline-field">
          <input type="text" id="filEditMaterial" class="filament-drawer-input" value="${app.commands.escapeHtml(app.commands._filamentMaterial(fil))}" placeholder="e.g. PLA">
        </div>
      `,
        { density: "compact" },
      )}
      ${app.commands.buildDrawerFormModule(
        "Hex Color",
        `
        <div class="filament-edit-field filament-inline-field">
          <div class="filament-hex-row">
            <input type="color" id="filEditPicker" value="${app.commands.escapeHtml(fil.hex)}">
            <input type="text" id="filEditHex" class="filament-drawer-input" value="${app.commands.escapeHtml(fil.hex)}" maxlength="7">
          </div>
        </div>
      `,
        { density: "compact" },
      )}
      ${app.commands.buildDrawerFormModule("Policy", app.commands._filamentOptionsHtml(fil), { density: "large", classes: "filament-policy-module" })}
      ${app.commands.buildDrawerFormModule(
        "Notes",
        `
        <textarea id="filEditNotes" class="sample-create-textarea filament-notes-input" rows="3" placeholder="Optional notes...">${app.commands.escapeHtml(app.commands._filamentNotes(fil))}</textarea>
      `,
        { density: "large" },
      )}
      ${app.commands.buildDrawerFormModule(`Used By ${usedBySection.count} Sample${usedBySection.count === 1 ? "" : "s"}`, usedBySection.html, { density: "table" })}
      ${app.commands.buildDrawerFormModule("ID", `<span class="mono drawer-form-value filament-slug-preview">${app.commands.escapeHtml(fil.filament_id)}</span>`, { density: "compact" })}
      <div class="filament-validation" id="filEditValidation"></div>
    `;
    app.commands.bindLinkedSampleTriggers(app.dom.detailSidebar);

    app.commands._bindFilamentEditControls(fil);
  }

  function _renderFilamentDrawerCreate() {
    app.commands.setDetailSidebarStackMode("form");
    app.commands.setDrawerHeading("New Filament");
    app.dom.drawerStatusPill.innerHTML = "";
    app.dom.detailWindowArea.innerHTML = "";
    app.dom.detailActionArea.innerHTML = `
      <button class="primary-button small drawer-header-action" id="filDrawerSaveBtn">Create</button>
    `;

    // Populate manufacturer suggestions
    const manufacturers = [
      ...new Set(
        app.state.session.data.filaments
          .map((f) => f.manufacturer)
          .filter(Boolean),
      ),
    ].sort();
    const mfrOptions = manufacturers
      .map((m) => `<option value="${app.commands.escapeHtml(m)}">`)
      .join("");

    app.dom.detailSidebar.innerHTML = `
      ${app.commands.buildDrawerFormModule(
        "Filament",
        `
        <div class="filament-header-row filament-header-row-edit">
          <span class="filament-drawer-chip" id="filEditChip" style="background:#888888"></span>
          <input type="text" id="filEditColorName" class="filament-inline-name-input filament-drawer-input" value="" placeholder="e.g. Basic Cyan" autocomplete="off">
        </div>
      `,
        { density: "compact" },
      )}
      ${app.commands.buildDrawerFormModule(
        "Manufacturer",
        `
        <div class="filament-edit-field filament-inline-field">
          <input type="text" id="filEditMfr" class="filament-drawer-input" value="" placeholder="e.g. Bambu" list="filMfrSuggestions" autocomplete="off">
          <datalist id="filMfrSuggestions">${mfrOptions}</datalist>
        </div>
      `,
        { density: "compact" },
      )}
      ${app.commands.buildDrawerFormModule(
        "Material",
        `
        <div class="filament-edit-field filament-inline-field">
          <input type="text" id="filEditMaterial" class="filament-drawer-input" value="unknown" placeholder="e.g. PLA" autocomplete="off">
        </div>
      `,
        { density: "compact" },
      )}
      ${app.commands.buildDrawerFormModule(
        "Hex Color",
        `
        <div class="filament-edit-field filament-inline-field">
          <div class="filament-hex-row">
            <input type="color" id="filEditPicker" value="#888888">
            <input type="text" id="filEditHex" class="filament-drawer-input" value="#888888" maxlength="7">
          </div>
        </div>
      `,
        { density: "compact" },
      )}
      ${app.commands.buildDrawerFormModule("Policy", app.commands._filamentOptionsHtml({ material: "unknown" }), { density: "large", classes: "filament-policy-module" })}
      ${app.commands.buildDrawerFormModule(
        "Notes",
        `
        <textarea id="filEditNotes" class="sample-create-textarea filament-notes-input" rows="3" placeholder="Optional notes..."></textarea>
      `,
        { density: "large" },
      )}
      ${app.commands.buildDrawerFormModule("Generated ID", `<span class="mono drawer-form-value filament-slug-preview" id="filCreateSlug">\u2014</span>`, { density: "compact" })}
      <div class="filament-validation" id="filEditValidation"></div>
    `;

    app.commands._bindFilamentCreateControls();
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
    const hasRefs = fil.sample_count > 0 || fil.has_profile;

    function syncChip() {
      const hex = app.commands.normalizeHexInput(hexInput.value);
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
      app.state.filaments._filamentDrawerMode = "view";
      app.commands._renderFilamentDrawerView(fil);
    });

    if (deleteBtn) {
      if (hasRefs) {
        deleteBtn.addEventListener("click", () => {
          app.commands.showInfoDialog(
            "Filaments cannot be deleted when they have samples associated with them.",
          );
        });
      } else {
        app.commands.bindConfirmAction(deleteBtn, {
          onConfirm: async () => {
            try {
              await app.api.deleteFilament(fil.filament_id);
              app.commands.showImportToast(
                `Deleted filament ${fil.filament_id}`,
                "success",
              );
              app.commands.closeDrawer();
              await app.commands.handleRefresh();
            } catch (err) {
              app.commands.showImportToast(
                err.message || "Failed to delete filament",
                "error",
              );
            }
          },
        });
      }
    }

    saveBtn.addEventListener("click", async () => {
      const mfr = mfrInput.value.trim();
      const cn = colorInput.value.trim();
      const hex = app.commands.normalizeHexInput(hexInput.value);
      const options = app.commands._readFilamentOptions();

      if (!mfr) {
        app.commands._setFilamentValidation(
          validation,
          "Manufacturer is required.",
          [mfrInput],
        );
        return;
      }
      if (!cn) {
        app.commands._setFilamentValidation(
          validation,
          "Filament name is required.",
          [colorInput],
        );
        return;
      }
      if (!hex) {
        app.commands._setFilamentValidation(
          validation,
          "Hex must be in #RRGGBB format.",
          [hexInput],
        );
        return;
      }
      app.commands._setFilamentValidation(validation, "");

      try {
        const resp = await fetch(
          `/api/filaments/${encodeURIComponent(fil.filament_id)}`,
          {
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
          },
        );
        if (!resp.ok) {
          const err = await resp
            .json()
            .catch(() => ({ detail: resp.statusText }));
          app.commands._setFilamentValidation(
            validation,
            err.detail || "Server error",
          );
          return;
        }
        app.commands.showProfileToast(`Updated ${mfr} ${cn}`);
        await app.commands.handleRefresh();
        // Re-fetch the updated filament from the refreshed data
        const updated = app.state.session.data.filaments.find(
          (f) => f.filament_id === fil.filament_id,
        );
        if (updated) {
          app.state.filaments._filamentDrawerMode = "view";
          app.state.filaments._filamentDrawerData = updated;
          app.commands._renderFilamentDrawerView(updated);
        }
      } catch (err) {
        app.commands._setFilamentValidation(
          validation,
          `Network error: ${err.message}`,
        );
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
      const hex = app.commands.normalizeHexInput(hexInput.value);
      if (hex) {
        chip.style.background = hex;
        picker.value = hex;
      }
    }

    function syncPreview() {
      const mfr = mfrInput.value.trim();
      const cn = colorInput.value.trim();
      const slug = app.commands._generateFilamentSlug(mfr, cn);
      slugEl.textContent = slug || "\u2014";

      if (
        slug &&
        app.state.session.data.filaments.some((f) => f.filament_id === slug)
      ) {
        app.commands._setFilamentValidation(
          validation,
          `ID "${slug}" already exists in the library.`,
        );
      } else {
        app.commands._setFilamentValidation(validation, "");
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
      const hex = app.commands.normalizeHexInput(hexInput.value);
      const options = app.commands._readFilamentOptions();

      if (!mfr) {
        app.commands._setFilamentValidation(
          validation,
          "Manufacturer is required.",
          [mfrInput],
        );
        return;
      }
      if (!cn) {
        app.commands._setFilamentValidation(
          validation,
          "Filament name is required.",
          [colorInput],
        );
        return;
      }
      if (!hex) {
        app.commands._setFilamentValidation(
          validation,
          "Hex must be in #RRGGBB format.",
          [hexInput],
        );
        return;
      }

      const slug = app.commands._generateFilamentSlug(mfr, cn);
      if (
        app.state.session.data.filaments.some((f) => f.filament_id === slug)
      ) {
        app.commands._setFilamentValidation(
          validation,
          `ID "${slug}" already exists.`,
          [colorInput],
        );
        return;
      }

      app.commands._setFilamentValidation(validation, "");
      try {
        const created = await app.api.createFilament(mfr, cn, hex, options);
        app.commands.showProfileToast(`Added ${created.display_name}`);
        app.commands.clearSelectionAndDrawer();
        app.dom.recordDrawer.classList.remove("narrow-drawer");
        app.state.filaments._filamentDrawerMode = null;
        app.state.filaments._filamentDrawerData = null;
        await app.commands.handleRefresh();
      } catch (err) {
        app.commands._setFilamentValidation(
          validation,
          err.message || "Server error",
        );
      }
    });
  }

  Object.assign(app.commands, {
    _filamentSpecialRoles,
    _filamentMaterial,
    _filamentNotes,
    _filamentRolesLabel,
    _filamentPolicyHtml,
    _filamentOptionsHtml,
    _readFilamentOptions,
    _setFilamentValidation,
    renderSidebarForFilament,
    openFilamentCreateDrawer,
    _renderFilamentDrawerView,
    _renderFilamentDrawerEdit,
    _renderFilamentDrawerCreate,
    _bindFilamentEditControls,
    _bindFilamentCreateControls,
  });
}
