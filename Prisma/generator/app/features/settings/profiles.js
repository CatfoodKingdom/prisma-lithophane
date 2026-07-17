/**
 * Install the settings/profiles feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesSettingsProfiles(app) {
  function _configSettingsProfileSnapshot() {
    app.commands.applyMandatoryProductSettings();
    const snap = {};
    for (const k of app.state.settings.SETTINGS_PROFILE_KEYS) snap[k] = app.commands._cloneValue(app.state.settings.config[k]);
    return snap;
  }

  function _cloneValue(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function _dropRetiredSettingsProfileKeys(settings) {
    const out = { ...(settings || {}) };
    const retiredSubject = "protect" + "_subject";
    const retiredMask = "protect" + "_mask";
    [
      `${retiredSubject}_enabled`,
      `${retiredSubject}_strength`,
      "protect" + "_confidence_floor",
      `${retiredMask}_provider`,
      `${retiredMask}_override`,
    ].forEach((key) => delete out[key]);
    return out;
  }

  function _normalizeSettingsProfileModules(modules = null, settings = {}) {
    const next = {};
    const source = modules || {};

    (app.state.settings.moduleData || []).forEach((m) => {
      if (Object.prototype.hasOwnProperty.call(source, m.name)) {
        next[m.name] = !!source[m.name];
      } else {
        next[m.name] = !!m.default_enabled;
      }
    });
    return next;
  }

  function _currentSettingsProfileModulesSnapshot() {
    return app.commands._normalizeSettingsProfileModules(app.state.settings.moduleState, app.state.settings.config);
  }

  function _settingsProfileModulesEqual(a = {}, b = {}) {
    const names = new Set([
      ...Object.keys(a || {}),
      ...Object.keys(b || {}),
      ...(app.state.settings.moduleData || []).map((m) => m.name),
    ]);
    for (const name of names) {
      if (!!a[name] !== !!b[name]) return false;
    }
    return true;
  }

  function _settingsProfileValuesEqual(a, b) {
    if (a === b) return true;
    if (a == null || b == null) return a === b;

    const aIsArray = Array.isArray(a);
    const bIsArray = Array.isArray(b);
    if (aIsArray || bIsArray) {
      if (!aIsArray || !bIsArray || a.length !== b.length) return false;
      for (let i = 0; i < a.length; i += 1) {
        if (!app.commands._settingsProfileValuesEqual(a[i], b[i])) return false;
      }
      return true;
    }

    if (typeof a === "object" || typeof b === "object") {
      if (typeof a !== "object" || typeof b !== "object") return false;
      const keys = new Set([
        ...Object.keys(a || {}),
        ...Object.keys(b || {}),
      ]);
      for (const key of keys) {
        if (!app.commands._settingsProfileValuesEqual(a?.[key], b?.[key])) return false;
      }
      return true;
    }

    return String(a) === String(b);
  }

  async function _applyModuleSnapshot(
    modules,
    settings = {},
    persist = true,
    { refreshViews = true } = {},
  ) {
    const normalized = app.commands._normalizeSettingsProfileModules(modules, settings);
    if (app.state.settings.moduleData.length === 0) {
      app.state.settings.moduleState = { ...normalized };
      return app.state.settings.moduleState;
    }

    if (persist && app.state.session.apiConnected) {
      const response = await app.api.setModuleState(normalized);
      app.state.settings.moduleState = response.state || normalized;
    } else {
      app.state.settings.moduleState = { ...normalized };
    }

    app.commands.syncConfigFromModuleState();
    app.commands.renderModulePanel();
    app.commands.renderDynamicSettings();
    if (refreshViews) app.commands.refreshModuleDrivenViews();
    return app.state.settings.moduleState;
  }

  function _applySettingsProfileToConfig(settings) {
    const source = app.commands._dropRetiredSettingsProfileKeys(settings);
    for (const k of app.state.settings.SETTINGS_PROFILE_KEYS) {
      let value = Object.prototype.hasOwnProperty.call(source, k)
        ? app.commands._cloneValue(source[k])
        : app.commands._cloneValue(app.state.settings.SETTINGS_PROFILE_DEFAULTS[k]);
      if (k === "gamut_mode") value = app.commands.normalizeActiveGamutMode(value);
      app.state.settings.config[k] = value;
    }
  }

  function _setLoadedSettingsProfile(record, snapshot = null) {
    app.state.settings.loadedProfileRef = record
      ? { id: record.id, kind: record.kind, name: record.name }
      : null;
    if (!record) {
      app.state.settings.loadedProfileSnapshot = null;
      return;
    }
    app.state.settings.loadedProfileSnapshot = app.commands._cloneValue(snapshot || {
      settings: app.commands._configSettingsProfileSnapshot(),
      modules: app.commands._currentSettingsProfileModulesSnapshot(),
    });
    app.state.settings.loadedProfileSnapshot.settings = app.commands._dropRetiredSettingsProfileKeys(
      app.state.settings.loadedProfileSnapshot.settings
    );
  }

  function allSettingsProfiles() {
    return app.state.settings.temporarySettingsProfile
      ? [app.state.settings.temporarySettingsProfile, ...app.state.settings.settingsProfiles]
      : [...app.state.settings.settingsProfiles];
  }

  function findSettingsProfile(profileId) {
    return app.commands.allSettingsProfiles().find((profile) => profile.id === profileId) || null;
  }

  function isSettingsProfileModified() {
    if (!app.state.settings.loadedProfileSnapshot) return false;
    const current = app.commands._currentSettingsSnapshot();
    for (const k of app.state.settings.SETTINGS_PROFILE_KEYS) {
      if (!app.commands._settingsProfileValuesEqual(current[k], app.state.settings.loadedProfileSnapshot.settings?.[k])) return true;
    }
    if (
      !app.commands._settingsProfileModulesEqual(
        app.commands._currentSettingsProfileModulesSnapshot(),
        app.state.settings.loadedProfileSnapshot.modules || {}
      )
    ) return true;
    return false;
  }

  function findSettingsProfileByName(name) {
    const trimmed = String(name || "").trim().toLocaleLowerCase();
    if (!trimmed) return null;
    return app.commands.allSettingsProfiles().find((profile) => (profile.name || "").trim().toLocaleLowerCase() === trimmed) || null;
  }

  function _settingsProfileBadges(profile, { modifiedLoaded = false } = {}) {
    if (!profile) return [];
    const badges = [];
    if (profile.kind === "temporary") {
      badges.push({ label: "TEMP", accent: false, warn: true });
    }
    if (profile.id === app.state.ui.SYSTEM_SETTINGS_PROFILE_ID) {
      badges.push({ label: "system", accent: false, warn: false });
    }
    if (profile.id === app.state.settings.userDefaultProfileId) {
      badges.push({ label: "startup", accent: true, warn: false });
    }
    return badges;
  }

  function _settingsProfileBadgesHtml(profile, { modifiedLoaded = false, mini = true } = {}) {
    return app.commands._settingsProfileBadges(profile, { modifiedLoaded }).map((badge) => {
      const classes = [
        mini ? "settings-profile-mini-badge" : "settings-profile-badge",
        badge.accent ? "is-accent" : "",
        badge.warn ? "is-warn" : "",
      ].filter(Boolean).join(" ");
      return `<span class="${classes}">${app.commands.esc(badge.label)}</span>`;
    }).join("");
  }

  function describeSettingsProfileNameInput(name, {
    currentProfileId = null,
    allowReplace = false,
  } = {}) {
    if (typeof name !== "string") {
      return { valid: false, error: "Settings Profile name must be text", trimmed: "", duplicate: null };
    }
    if (name.trim().length === 0) {
      return { valid: false, error: "Settings Profile name cannot be empty", trimmed: "", duplicate: null };
    }
    if (name !== name.trim()) {
      return {
        valid: false,
        error: "Settings Profile name cannot start or end with whitespace",
        trimmed: name.trim(),
        duplicate: null,
      };
    }

    const trimmed = name.trim();
    if (trimmed.endsWith(".")) {
      return { valid: false, error: "Settings Profile name cannot end with a period", trimmed, duplicate: null };
    }
    for (const ch of trimmed) {
      if (app.state.settings.SETTINGS_PROFILE_FORBIDDEN_NAME_CHARS.has(ch) || ch.charCodeAt(0) < 32) {
        return {
          valid: false,
          error: 'Settings Profile name cannot contain < > : " / \\ | ? *',
          trimmed,
          duplicate: null,
        };
      }
    }

    const duplicate = app.state.settings.settingsProfiles.find((profile) => {
      if (profile.id === currentProfileId) return false;
      return (profile.name || "").trim().toLocaleLowerCase() === trimmed.toLocaleLowerCase();
    }) || null;

    if (!duplicate) {
      return { valid: true, error: null, trimmed, duplicate: null, replaceExisting: false };
    }
    if (!allowReplace) {
      return {
        valid: false,
        error: `A Settings Profile named "${trimmed}" already exists`,
        trimmed,
        duplicate,
        replaceExisting: false,
      };
    }
    if (duplicate.kind !== "named") {
      return {
        valid: false,
        error: `The system Settings Profile "${trimmed}" cannot be replaced`,
        trimmed,
        duplicate,
        replaceExisting: false,
      };
    }
    return {
      valid: true,
      error: null,
      trimmed,
      duplicate,
      replaceExisting: true,
    };
  }

  function _refreshSettingsProfilesFromResponse(data) {
    app.state.settings.settingsProfiles = Array.isArray(data?.profiles) ? data.profiles : [];
    app.state.settings.userDefaultProfileId = data?.user_default_profile_id || app.state.ui.SYSTEM_SETTINGS_PROFILE_ID;
  }

  function validateSettingsProfileNameLocal(name, currentProfileId = null) {
    return app.commands.describeSettingsProfileNameInput(name, { currentProfileId }).error;
  }

  function _captureLiveSettingsProfileState() {
    return {
      config: app.commands._configSettingsProfileSnapshot(),
      modules: app.commands._currentSettingsProfileModulesSnapshot(),
      loadedProfileRef: app.commands._cloneValue(app.state.settings.loadedProfileRef),
      loadedProfileSnapshot: app.commands._cloneValue(app.state.settings.loadedProfileSnapshot),
      temporarySettingsProfile: app.commands._cloneValue(app.state.settings.temporarySettingsProfile),
    };
  }

  async function _restoreLiveSettingsProfileState(snapshot, { syncServer = true } = {}) {
    app.commands._applySettingsProfileToConfig(snapshot.config || {});
    await app.commands._applyModuleSnapshot(
      snapshot.modules || {},
      snapshot.config || {},
      false,
      { refreshViews: false },
    );
    app.state.settings.temporarySettingsProfile = app.commands._cloneValue(snapshot.temporarySettingsProfile);
    app.state.settings.loadedProfileRef = app.commands._cloneValue(snapshot.loadedProfileRef);
    app.state.settings.loadedProfileSnapshot = app.commands._cloneValue(snapshot.loadedProfileSnapshot);
    app.commands.renderSettingsTab();
    if (syncServer && app.state.session.apiConnected) {
      await app.commands.syncConfigToServer({ throwOnError: true });
      await app.commands._applyModuleSnapshot(
        snapshot.modules || {},
        snapshot.config || {},
        true,
        { refreshViews: false },
      );
    }
    app.commands.renderSettingsProfileBar();
  }

  function _runSettingsMetadata(body) {
    const metadata = body?.run_metadata && typeof body.run_metadata === "object"
      ? body.run_metadata
      : {};
    // Live solve cards keep the same recipe snapshot on the card itself, while
    // archived runs expose it through the optional run_metadata envelope.
    const directRecipeSnapshot = body?.recipe_snapshot
      && typeof body.recipe_snapshot === "object"
      ? body.recipe_snapshot
      : {};
    const durableSnapshot = metadata.recipe_snapshot?.profile_snapshot
      && typeof metadata.recipe_snapshot.profile_snapshot === "object"
      ? metadata.recipe_snapshot.profile_snapshot
      : directRecipeSnapshot.profile_snapshot
        && typeof directRecipeSnapshot.profile_snapshot === "object"
        ? directRecipeSnapshot.profile_snapshot
        : {};
    const diagnostics = metadata.solve_start_diagnostics
      && typeof metadata.solve_start_diagnostics === "object"
      ? metadata.solve_start_diagnostics
      : body?.result?.solve_start_diagnostics
        && typeof body.result.solve_start_diagnostics === "object"
        ? body.result.solve_start_diagnostics
        : body?.results?.solve_start_diagnostics
          && typeof body.results.solve_start_diagnostics === "object"
          ? body.results.solve_start_diagnostics
          : {};
    return { metadata, durableSnapshot, diagnostics };
  }

  function _settingsSnapshotFromRunPayload(body) {
    const { metadata, durableSnapshot, diagnostics } = app.commands._runSettingsMetadata(body);
    const source = {
      ...(body?.config && typeof body.config === "object" ? body.config : {}),
      ...(metadata.config && typeof metadata.config === "object" ? metadata.config : {}),
      ...(diagnostics.resolved_settings && typeof diagnostics.resolved_settings === "object"
        ? diagnostics.resolved_settings : {}),
      ...(durableSnapshot.settings && typeof durableSnapshot.settings === "object"
        ? durableSnapshot.settings : {}),
    };
    if (source.luminance_base_shading_limit_fraction == null
        && source.luminance_handler_optical_authority_fraction != null) {
      source.luminance_base_shading_limit_fraction = source.luminance_handler_optical_authority_fraction;
    }
    const settings = {};
    for (const key of app.state.settings.SETTINGS_PROFILE_KEYS) {
      settings[key] = Object.prototype.hasOwnProperty.call(source, key)
        ? app.commands._cloneValue(source[key])
        : app.commands._cloneValue(app.state.settings.SETTINGS_PROFILE_DEFAULTS[key]);
    }
    const preprocessingParams = {
      ...(source.preprocessing_params && typeof source.preprocessing_params === "object"
        ? app.commands._cloneValue(source.preprocessing_params) : {}),
    };
    const diagnosticModuleSettings = diagnostics.module_settings
      && typeof diagnostics.module_settings === "object"
      ? diagnostics.module_settings
      : {};
    for (const [moduleId, values] of Object.entries(diagnosticModuleSettings)) {
      if (!values || typeof values !== "object") continue;
      preprocessingParams[moduleId] = {
        ...(preprocessingParams[moduleId] || {}),
        ...app.commands._cloneValue(values),
      };
    }
    settings.preprocessing_params = preprocessingParams;
    return {
      settings: app.commands._dropRetiredSettingsProfileKeys(settings),
      metadata,
      durableSnapshot,
      diagnostics,
    };
  }

  function _modulesSnapshotFromRunPayload(settings, durableSnapshot, diagnostics) {
    const durableModules = durableSnapshot.modules && typeof durableSnapshot.modules === "object"
      ? durableSnapshot.modules
      : null;
    const diagnosticModules = diagnostics.module_state && typeof diagnostics.module_state === "object"
      ? diagnostics.module_state
      : null;
    const normalized = app.commands._normalizeSettingsProfileModules(
      durableModules || diagnosticModules || null,
      settings,
    );
    // A durable recipe snapshot is the captured frontend truth. Diagnostics are
    // only a compatibility fallback for older runs that predate that snapshot.
    const active = !durableModules
      && diagnostics.active_modules && typeof diagnostics.active_modules === "object"
      ? diagnostics.active_modules
      : {};
    for (const [slot, names] of Object.entries(active)) {
      if (!Array.isArray(names)) continue;
      const slotModules = (app.state.settings.moduleData || []).filter((module) => module.slot === slot);
      if (!slotModules.length) continue;
      slotModules.forEach((module) => { normalized[module.name] = false; });
      names.forEach((name) => {
        if (Object.prototype.hasOwnProperty.call(normalized, name)) normalized[name] = true;
      });
    }
    return normalized;
  }

  function buildTemporarySettingsProfileFromRun(body, source = {}) {
    const extracted = app.commands._settingsSnapshotFromRunPayload(body);
    const label = String(
      source.label || body?.label || extracted.durableSnapshot.name || "Solved run settings",
    ).trim() || "Solved run settings";
    const metadata = extracted.metadata || {};
    const profile = {
      id: "temporary-run-settings",
      kind: "temporary",
      name: label,
      settings: extracted.settings,
      modules: app.commands._modulesSnapshotFromRunPayload(
        extracted.settings,
        extracted.durableSnapshot,
        extracted.diagnostics,
      ),
      source: {
        kind: source.kind || (source.save_id ? "saved-run" : "solve-card"),
        save_id: source.save_id || null,
        tier: source.tier || null,
        run_id: source.run_id || body?.card_id || null,
        label,
      },
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    profile.profile_ref = app.commands._cloneValue(
      metadata.profile_ref
        || body?.profile_ref
        || extracted.durableSnapshot.profile_ref
        || null,
    );
    return profile;
  }

  async function _doLoadSettingsProfile(profile, { syncServer = true } = {}) {
    if (!profile) return false;
    if (syncServer && app.state.session.apiConnected) {
      await app.state.settings._configSyncChain.catch(() => {});
    }
    const previous = app.commands._captureLiveSettingsProfileState();
    let configSynced = false;
    try {
      app.commands._applySettingsProfileToConfig(profile.settings || {});
      await app.commands._applyModuleSnapshot(
        profile.modules,
        profile.settings || {},
        false,
        { refreshViews: false },
      );
      app.commands.renderSettingsTab();
      if (syncServer && app.state.session.apiConnected) {
        await app.commands.syncConfigToServer({ throwOnError: true });
        configSynced = true;
        await app.commands._applyModuleSnapshot(
          profile.modules,
          profile.settings || {},
          true,
          { refreshViews: false },
        );
      }
    } catch (error) {
      try {
        await app.commands._restoreLiveSettingsProfileState(previous, { syncServer: configSynced });
      } catch (restoreError) {
        console.error("[settings profiles] rollback failed:", restoreError);
      }
      throw error;
    }
    app.state.settings.temporarySettingsProfile = profile.kind === "temporary" ? app.commands._cloneValue(profile) : null;
    app.commands._setLoadedSettingsProfile(profile, {
      settings: app.commands._configSettingsProfileSnapshot(),
      modules: app.commands._currentSettingsProfileModulesSnapshot(),
    });
    app.commands.renderSettingsProfileBar();
    return true;
  }

  async function _loadTemporarySettingsFromRun(body, source = {}) {
    const profile = app.commands.buildTemporarySettingsProfileFromRun(body, source);
    const actionLabel = `loading settings from “${profile.source.label}”`;
    const proceed = await app.commands._guardSettingsProfileTransition(actionLabel);
    if (!proceed) return false;
    await app.commands._doLoadSettingsProfile(profile);
    app.commands.showToast(`Loaded settings from “${profile.source.label}” as TEMP`, "success");
    return true;
  }

  async function loadSettingsProfiles() {
    try {
      const data = await app.api.fetchSettingsProfiles();
      app.commands._refreshSettingsProfilesFromResponse(data);
      const preferredProfile = app.commands.findSettingsProfile(app.state.settings.userDefaultProfileId)
        || app.commands.findSettingsProfile(data?.system_profile_id || app.state.ui.SYSTEM_SETTINGS_PROFILE_ID)
        || app.state.settings.settingsProfiles[0]
        || null;
      if (preferredProfile) {
        await app.commands._doLoadSettingsProfile(preferredProfile, { syncServer: true });
      }
    } catch (e) {
      console.warn("[settings profiles] load failed:", e.message);
      if (!app.state.settings.loadedProfileSnapshot) {
        app.commands._setLoadedSettingsProfile({
          id: app.state.ui.SYSTEM_SETTINGS_PROFILE_ID,
          kind: "system",
          name: app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME,
        }, {
          settings: app.commands._configSettingsProfileSnapshot(),
          modules: app.commands._currentSettingsProfileModulesSnapshot(),
        });
      }
    }
    app.commands.renderSettingsTab();
  }

  async function loadPresets() {
    return app.commands.loadSettingsProfiles();
  }

  function renderSettingsProfileBar() {
    const profile = app.state.settings.loadedProfileRef ? app.commands.findSettingsProfile(app.state.settings.loadedProfileRef.id) || app.state.settings.loadedProfileRef : null;
    const modified = app.commands.isSettingsProfileModified();
    const nameEl = app.state.ui.$("#settingsProfileName");
    const statusEl = app.state.ui.$("#settingsProfileStatus");
    const sourceEl = app.state.ui.$("#settingsProfileSource");

    if (nameEl) nameEl.textContent = profile?.name || app.state.ui.SYSTEM_SETTINGS_PROFILE_NAME;
    if (sourceEl) {
      sourceEl.textContent = profile?.kind === "temporary"
        ? `From solved run: ${profile.source?.label || profile.name || "unknown"}`
        : "";
      sourceEl.classList.toggle("is-hidden", profile?.kind !== "temporary");
    }
    if (statusEl) {
      const badges = [];
      badges.push(app.commands._settingsProfileBadgesHtml(profile, { mini: false }));
      if (modified) {
        badges.push('<span class="settings-profile-badge is-warn">modified</span>');
      }
      statusEl.innerHTML = badges.filter(Boolean).join("");
    }

    const saveBtn = app.state.ui.$("#settingsProfileSaveBtn");
    if (saveBtn) {
      saveBtn.disabled = !modified;
      saveBtn.title = modified ? "" : "No changes to save";
    }
  }

  function checkPresetModified() {
    app.commands.renderSettingsProfileBar();
  }

  function _renderSettingsProfileList(listEl, selectedId, {
    allowSelectLoaded = true,
    showActions = true,
    editingProfileId = null,
    editingName = "",
  } = {}) {
    listEl.innerHTML = app.commands.allSettingsProfiles().map((profile) => {
      const isLoaded = app.state.settings.loadedProfileRef?.id === profile.id;
      const isEditing = editingProfileId === profile.id;
      const canRename = profile.kind === "named";
      const canDelete = profile.kind === "named" && !isLoaded;
      const isStartup = profile.id === app.state.settings.userDefaultProfileId;
      const canSetStartup = profile.kind !== "temporary" && !isStartup;
      const sourceLabel = profile.kind === "temporary" && profile.source?.label
        ? `<span class="settings-profile-modal-item-source">From ${app.commands.esc(profile.source.label)}</span>`
        : "";
      const itemClasses = [
        "settings-profile-modal-item",
        selectedId === profile.id ? "is-selected" : "",
        isLoaded ? "is-current" : "",
        isEditing ? "is-editing" : "",
        !allowSelectLoaded && isLoaded ? "is-disabled" : "",
      ].filter(Boolean).join(" ");
      return `
        <div
          class="${itemClasses}"
          data-profile-id="${app.commands.esc(profile.id)}"
          tabindex="0"
          role="button"
        >
          <span class="settings-profile-modal-item-main">
            <span class="settings-profile-modal-item-head">
              ${isEditing ? `
                <input
                  type="text"
                  class="control-input settings-profile-inline-name-input"
                  data-profile-rename-input="${app.commands.esc(profile.id)}"
                  value="${app.commands.esc(editingName)}"
                  aria-label="Rename profile"
                  autocomplete="off"
                  spellcheck="false"
                >
              ` : `
                <span class="settings-profile-modal-item-name">${app.commands.esc(profile.name)}</span>
              `}
            </span>
            ${sourceLabel}
            <span class="settings-profile-modal-item-badges">${app.commands._settingsProfileBadgesHtml(profile)}</span>
          </span>
          ${showActions ? `
            <span class="settings-profile-modal-item-actions">
              ${isEditing ? `
                <button type="button" class="ghost-button xxs settings-profile-icon-btn" data-profile-action="rename_save" data-profile-id="${app.commands.esc(profile.id)}" title="Save name" aria-label="Save name">
                  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.2 8.3 6.4 11.4 12.8 4.8"></path></svg>
                </button>
                <button type="button" class="ghost-button xxs settings-profile-icon-btn" data-profile-action="rename_cancel" data-profile-id="${app.commands.esc(profile.id)}" title="Cancel rename" aria-label="Cancel rename">
                  <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4 4 12"></path></svg>
                </button>
              ` : `
                ${canRename ? `
                  <button type="button" class="ghost-button xxs settings-profile-icon-btn" data-profile-action="rename" data-profile-id="${app.commands.esc(profile.id)}" title="Rename profile" aria-label="Rename profile">
                    <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 11.5V13h1.5L11.8 5.7 10.3 4.2 3 11.5Z"></path><path d="M9.9 4.6 11.4 6.1"></path></svg>
                  </button>
                ` : ""}
                ${profile.kind !== "temporary" ? `
                  <button type="button" class="ghost-button xxs settings-profile-icon-btn${canSetStartup ? "" : " is-active"}" data-profile-action="set_default" data-profile-id="${app.commands.esc(profile.id)}" title="${canSetStartup ? "Set as startup default" : "Startup default"}" aria-label="${canSetStartup ? "Set as startup default" : "Startup default"}">
                    <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m8 2 1.8 3.7 4.2.6-3 2.9.7 4.2L8 11.5l-3.7 1.9.7-4.2-3-2.9 4.2-.6L8 2Z"></path></svg>
                  </button>
                ` : ""}
                ${canDelete ? `
                  <button type="button" class="ghost-button xxs danger settings-profile-icon-btn" data-profile-action="delete" data-profile-id="${app.commands.esc(profile.id)}" title="Delete profile" aria-label="Delete profile">
                    <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4 4 12"></path></svg>
                  </button>
                ` : ""}
              `}
            </span>
          ` : ""}
        </div>
      `;
    }).join("");
  }

  function _settingsProfileSelectionHtml(profile) {
    const isLoaded = app.state.settings.loadedProfileRef?.id === profile?.id;
    const modified = app.commands.isSettingsProfileModified();
    const loadLabel = isLoaded && modified ? "Reload Saved Version" : "Load";
    const loadDisabled = !profile || (isLoaded && !modified);
    const source = profile?.kind === "temporary"
      ? `From solved run: ${profile.source?.label || profile.name || "unknown"}`
      : "";

    return `
      <div class="settings-profile-modal-selection-field">
        <div class="settings-profile-modal-selection-label">Selected Profile</div>
        <div class="settings-profile-modal-selection-value${profile ? "" : " is-empty"}">${profile ? app.commands.esc(profile.name) : ""}</div>
        ${source ? `<div class="settings-profile-modal-selection-source">${app.commands.esc(source)}</div>` : ""}
      </div>
      <div class="settings-profile-modal-selection-actions">
        <button class="primary-button" data-browser-action="load"${loadDisabled ? " disabled" : ""}>${app.commands.esc2(loadLabel)}</button>
        <button class="ghost-button" data-browser-action="cancel">Cancel</button>
      </div>
    `;
  }

  async function showSettingsProfileBrowserModal({
    title = "Settings Profiles",
    selectedProfileId = null,
    onAction = null,
  } = {}) {
    return new Promise((resolve) => {
      const overlay = app.state.ui.$("#settingsProfileModal");
      const titleEl = app.state.ui.$("#settingsProfileModalTitle");
      const listEl = app.state.ui.$("#settingsProfileModalList");
      const selectionEl = app.state.ui.$("#settingsProfileModalSelection");
      const closeBtn = app.state.ui.$("#settingsProfileModalClose");
      const loadRunBtn = app.state.ui.$("#settingsProfileModalLoadRunBtn");
      const restoreBtn = app.state.ui.$("#settingsProfileModalRestoreBtn");
      if (!overlay || !titleEl || !listEl || !selectionEl || !closeBtn || !loadRunBtn || !restoreBtn) {
        resolve(null);
        return;
      }

      let selectedId = selectedProfileId;
      let pendingDeleteId = null;
      let editingProfileId = null;
      let editingName = "";
      let focusRenameInput = false;
      let busy = false;

      const cancelInlineRename = () => {
        editingProfileId = null;
        editingName = "";
        focusRenameInput = false;
      };

      const currentRenameState = () => {
        if (!editingProfileId) return null;
        const profile = app.commands.findSettingsProfile(editingProfileId);
        if (!profile) return null;
        const described = app.commands.describeSettingsProfileNameInput(editingName, { currentProfileId: editingProfileId });
        return {
          profile,
          ...described,
          unchanged: described.trimmed === profile.name,
        };
      };

      const syncInlineRenameUi = () => {
        const input = listEl.querySelector("[data-profile-rename-input]");
        const saveBtn = listEl.querySelector('[data-profile-action="rename_save"]');
        if (!input) return;
        const state = currentRenameState();
        const error = state?.error || "";
        const disableSave = !state || !state.valid || state.unchanged;
        input.classList.toggle("is-invalid", !!error);
        input.setAttribute("aria-invalid", error ? "true" : "false");
        input.title = error || "";
        if (saveBtn) {
          saveBtn.disabled = disableSave;
          saveBtn.title = error || (state?.unchanged ? "Name unchanged" : "Save name");
          saveBtn.setAttribute("aria-label", error || (state?.unchanged ? "Name unchanged" : "Save name"));
        }
      };

      const commitInlineRename = async () => {
        const state = currentRenameState();
        if (!state || !state.profile) return false;
        if (state.unchanged) {
          cancelInlineRename();
          render();
          return false;
        }
        if (!state.valid) {
          syncInlineRenameUi();
          const input = listEl.querySelector("[data-profile-rename-input]");
          input?.focus();
          input?.select();
          return false;
        }
        const response = await app.api.updateSettingsProfile(state.profile.id, {
          name: state.trimmed,
          settings: state.profile.settings,
          modules: state.profile.modules,
        });
        app.commands._refreshSettingsProfilesFromResponse(response);
        const updated = app.commands.findSettingsProfile(state.profile.id);
        if (updated && app.state.settings.loadedProfileRef?.id === updated.id) {
          app.state.settings.loadedProfileRef = { ...app.state.settings.loadedProfileRef, name: updated.name };
        }
        app.commands.renderSettingsTab({ preservePendingUi: true });
        cancelInlineRename();
        app.commands.showToast(`Renamed Settings Profile to "${state.trimmed}"`, "success");
        render();
        return true;
      };

      const renderList = () => {
        app.commands._renderSettingsProfileList(listEl, selectedId, {
          editingProfileId,
          editingName,
        });

        listEl.querySelectorAll(".settings-profile-modal-item").forEach((button) => {
          button.onclick = (event) => {
            if (event.target.closest("[data-profile-action]")) return;
            if (event.target.closest("[data-profile-rename-input]")) return;
            if (editingProfileId) cancelInlineRename();
            selectedId = button.dataset.profileId || null;
            pendingDeleteId = null;
            render();
          };
          button.onkeydown = (event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              if (editingProfileId) cancelInlineRename();
              selectedId = button.dataset.profileId || null;
              pendingDeleteId = null;
              render();
            }
          };
        });
        const renameInput = listEl.querySelector("[data-profile-rename-input]");
        if (renameInput) {
          renameInput.oninput = () => {
            editingName = renameInput.value;
            syncInlineRenameUi();
          };
          renameInput.onkeydown = async (event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              if (busy) return;
              busy = true;
              try {
                await commitInlineRename();
              } finally {
                busy = false;
              }
              return;
            }
            if (event.key === "Escape") {
              event.preventDefault();
              cancelInlineRename();
              render();
            }
          };
          syncInlineRenameUi();
          if (focusRenameInput) {
            focusRenameInput = false;
            setTimeout(() => {
              renameInput.focus();
              renameInput.select();
            }, 0);
          }
        }
        listEl.querySelectorAll("[data-profile-action]").forEach((button) => {
          button.onclick = async (event) => {
            event.stopPropagation();
            if (busy) return;
            const action = button.dataset.profileAction;
            const profileId = button.dataset.profileId || null;
            if (action === "rename") {
              const profile = app.commands.findSettingsProfile(profileId);
              if (!profile) return;
              editingProfileId = profileId;
              editingName = profile.name;
              selectedId = profileId;
              pendingDeleteId = null;
              focusRenameInput = true;
              render();
              return;
            }
            if (action === "rename_cancel") {
              cancelInlineRename();
              render();
              return;
            }
            if (action === "rename_save") {
              busy = true;
              try {
                await commitInlineRename();
              } finally {
                busy = false;
              }
              return;
            }
            if (action === "delete") {
              if (pendingDeleteId === profileId) {
                if (typeof onAction === "function") {
                  busy = true;
                  try {
                    const outcome = await onAction({ action, profileId, selectedProfileId: selectedId });
                    selectedId = outcome?.selectedProfileId || selectedId;
                    pendingDeleteId = null;
                    if (outcome?.close) {
                      close(outcome.result || null);
                      return;
                    }
                    render();
                  } finally {
                    busy = false;
                  }
                  return;
                }
                close({ action, profileId });
                return;
              }
              pendingDeleteId = profileId;
              render();
              return;
            }
            if (editingProfileId) cancelInlineRename();
            if (typeof onAction === "function") {
              busy = true;
              try {
                const outcome = await onAction({ action, profileId, selectedProfileId: selectedId });
                selectedId = outcome?.selectedProfileId || selectedId;
                pendingDeleteId = null;
                if (outcome?.close) {
                  close(outcome.result || null);
                  return;
                }
                render();
              } finally {
                busy = false;
              }
              return;
            }
            pendingDeleteId = null;
            close({ action, profileId });
          };
          if (button.dataset.profileAction === "delete" && pendingDeleteId === button.dataset.profileId) {
            button.classList.add("is-pending-delete");
            button.title = "Click again to delete";
            button.setAttribute("aria-label", "Click again to delete");
          }
        });
      };

      const renderSelection = () => {
        const profile = app.commands.findSettingsProfile(selectedId);
        selectionEl.innerHTML = app.commands._settingsProfileSelectionHtml(profile);
        selectionEl.querySelectorAll("[data-browser-action]").forEach((button) => {
          button.onclick = () => {
            const action = button.dataset.browserAction;
            if (action === "cancel") {
              close(null);
              return;
            }
            close({ action, profileId: selectedId });
          };
        });
      };

      const render = () => {
        renderList();
        renderSelection();
      };

      const close = (result) => {
        overlay.classList.add("is-hidden");
        overlay.setAttribute("aria-hidden", "true");
        resolve(result);
      };

      titleEl.textContent = title || "Settings Profiles";
      render();

      overlay.classList.remove("is-hidden");
      overlay.setAttribute("aria-hidden", "false");

      restoreBtn.onclick = () => close({ action: "restore", profileId: selectedId });
      loadRunBtn.onclick = () => close({ action: "load_saved_run", profileId: selectedId });
      closeBtn.onclick = () => close(null);
      overlay.onclick = (event) => {
        if (event.target === overlay) close(null);
      };
    });
  }

  async function showSettingsProfileSaveAsModal({
    title = "Save Settings Profile As",
    defaultValue = "",
    currentProfileId = null,
  } = {}) {
    return new Promise((resolve) => {
      const overlay = app.state.ui.$("#settingsProfileSaveModal");
      const titleEl = app.state.ui.$("#settingsProfileSaveModalTitle");
      const listEl = app.state.ui.$("#settingsProfileSaveList");
      const inputEl = app.state.ui.$("#settingsProfileSaveName");
      const statusEl = app.state.ui.$("#settingsProfileSaveStatus");
      const submitBtn = app.state.ui.$("#settingsProfileSaveModalSubmit");
      const closeBtn = app.state.ui.$("#settingsProfileSaveModalClose");
      const cancelBtn = app.state.ui.$("#settingsProfileSaveModalCancel");
      if (!overlay || !titleEl || !listEl || !inputEl || !statusEl || !submitBtn || !closeBtn || !cancelBtn) {
        resolve(null);
        return;
      }

      let submission = null;
      let selectedId = null;

      const renderList = () => {
        app.commands._renderSettingsProfileList(listEl, selectedId, { showActions: false });
        listEl.querySelectorAll(".settings-profile-modal-item").forEach((button) => {
          button.onclick = () => {
            const profile = app.commands.findSettingsProfile(button.dataset.profileId || "");
            if (!profile) return;
            selectedId = profile.id;
            inputEl.value = profile.name || "";
            renderList();
            renderStatus();
            inputEl.focus();
            inputEl.select();
          };
        });
      };

      const renderStatus = () => {
        const state = app.commands.describeSettingsProfileNameInput(inputEl.value, {
          currentProfileId,
          allowReplace: true,
        });
        submission = state.valid
          ? {
              name: state.trimmed,
              replaceProfileId: state.replaceExisting ? state.duplicate?.id || null : null,
            }
          : null;

        if (!inputEl.value.trim()) {
          statusEl.className = "settings-profile-save-status";
          statusEl.innerHTML = "";
          submitBtn.disabled = true;
          submitBtn.textContent = "Save";
          return;
        }

        if (!state.valid) {
          statusEl.className = "settings-profile-save-status is-error";
          statusEl.textContent = state.error;
          submitBtn.disabled = true;
          submitBtn.textContent = "Save";
          return;
        }

        if (state.replaceExisting && state.duplicate) {
          statusEl.className = "settings-profile-save-status";
          statusEl.textContent = "";
          submitBtn.disabled = false;
          submitBtn.textContent = "Overwrite";
          return;
        }

        statusEl.className = "settings-profile-save-status";
        statusEl.innerHTML = "";
        submitBtn.disabled = false;
        submitBtn.textContent = "Save";
      };

      const close = (result) => {
        overlay.classList.add("is-hidden");
        overlay.setAttribute("aria-hidden", "true");
        resolve(result);
      };

      titleEl.textContent = title;
      inputEl.value = defaultValue || "";
      selectedId = app.commands.findSettingsProfileByName(defaultValue || "")?.id || null;
      renderList();
      renderStatus();

      overlay.classList.remove("is-hidden");
      overlay.setAttribute("aria-hidden", "false");

      inputEl.oninput = () => {
        selectedId = app.commands.findSettingsProfileByName(inputEl.value || "")?.id || null;
        renderList();
        renderStatus();
      };
      inputEl.onkeydown = (event) => {
        if (event.key === "Enter" && submission) {
          event.preventDefault();
          close(submission);
        }
      };
      submitBtn.onclick = () => {
        if (submission) close(submission);
      };
      cancelBtn.onclick = () => close(null);
      closeBtn.onclick = () => close(null);
      overlay.onclick = (event) => {
        if (event.target === overlay) close(null);
      };

      setTimeout(() => {
        inputEl.focus();
        inputEl.select();
      }, 0);
    });
  }

  async function _guardSettingsProfileTransition(actionLabel = "loading another Settings Profile") {
    if (!app.commands.isSettingsProfileModified()) return true;
    const choice = await app.commands.appChoice(
      `This Settings Profile has unsaved changes. What would you like to do before ${actionLabel}?`,
      [
        { label: "Save As", value: "save_as", kind: "primary" },
        { label: "Discard", value: "discard" },
        { label: "Cancel", value: "cancel" },
      ],
      { title: "Unsaved Settings Profile" },
    );
    if (choice === "discard") return true;
    if (choice === "save_as") {
      return !!(await app.commands._ensureDraftSavedAsSettingsProfile(
        app.state.settings.loadedProfileRef?.kind === "named" ? app.state.settings.loadedProfileRef.name : ""
      ));
    }
    return false;
  }

  async function _saveDraftAsSettingsProfileWithName(name) {
    const beforeIds = new Set(app.state.settings.settingsProfiles.map((profile) => profile.id));
    const response = await app.api.createSettingsProfile({
      name,
      settings: app.commands._currentSettingsSnapshot(),
      modules: app.commands._currentSettingsProfileModulesSnapshot(),
    });
    app.commands._refreshSettingsProfilesFromResponse(response);
    const created = app.state.settings.settingsProfiles.find((profile) => !beforeIds.has(profile.id))
      || app.state.settings.settingsProfiles.find((profile) => (profile.name || "").toLocaleLowerCase() === name.toLocaleLowerCase())
      || null;
    if (created) {
      app.state.settings.temporarySettingsProfile = null;
      app.commands._setLoadedSettingsProfile(created, {
        settings: app.commands._currentSettingsSnapshot(),
        modules: app.commands._currentSettingsProfileModulesSnapshot(),
      });
    }
    app.commands.renderSettingsTab({ preservePendingUi: true });
    return created;
  }

  async function _overwriteSettingsProfile(profile, { nameOverride = null } = {}) {
    if (!profile || profile.kind !== "named") return null;
    const response = await app.api.updateSettingsProfile(profile.id, {
      name: nameOverride || profile.name,
      settings: app.commands._currentSettingsSnapshot(),
      modules: app.commands._currentSettingsProfileModulesSnapshot(),
    });
    app.commands._refreshSettingsProfilesFromResponse(response);
    const updated = app.commands.findSettingsProfile(profile.id);
    if (updated) {
      app.state.settings.temporarySettingsProfile = null;
      app.commands._setLoadedSettingsProfile(updated, {
        settings: app.commands._currentSettingsSnapshot(),
        modules: app.commands._currentSettingsProfileModulesSnapshot(),
      });
    }
    app.commands.renderSettingsTab({ preservePendingUi: true });
    return updated;
  }

  async function _overwriteLoadedNamedSettingsProfile(nameOverride = null) {
    const current = app.state.settings.loadedProfileRef ? app.commands.findSettingsProfile(app.state.settings.loadedProfileRef.id) : null;
    return app.commands._overwriteSettingsProfile(current, { nameOverride });
  }

  async function _ensureDraftSavedAsSettingsProfile(defaultName = "") {
    const result = await app.commands.showSettingsProfileSaveAsModal({
      title: "Save Settings Profile As",
      defaultValue: defaultName,
      currentProfileId: null,
    });
    if (!result) return null;

    if (result.replaceProfileId) {
      const existing = app.commands.findSettingsProfile(result.replaceProfileId);
      if (!existing) {
        app.commands.showToast("That Settings Profile is no longer available", "error");
        return null;
      }
      const confirmed = await app.commands.appConfirm(
        `Replace "${existing.name}" with the current draft?`,
        { ok: "Replace", cancel: "Cancel" }
      );
      if (!confirmed) return null;
      const updated = await app.commands._overwriteSettingsProfile(existing);
      if (updated) app.commands.showToast(`Settings Profile "${updated.name}" saved`, "success");
      return updated;
    }

    const created = await app.commands._saveDraftAsSettingsProfileWithName(result.name);
    if (created) app.commands.showToast(`Settings Profile "${created.name}" created`, "success");
    return created;
  }

  async function handleSettingsProfileDelete(profileId) {
    const profile = app.commands.findSettingsProfile(profileId);
    if (!profile) return false;
    if (profile.kind !== "named") {
      app.commands.showToast("The system default profile cannot be deleted", "error");
      return false;
    }
    if (app.state.settings.loadedProfileRef?.id === profile.id) {
      app.commands.showToast("Load another profile before deleting this one", "error");
      return false;
    }
    const confirmed = await app.commands.appConfirm(
      `Delete the Settings Profile "${profile.name}"?`,
      { ok: "Delete", cancel: "Cancel" }
    );
    if (!confirmed) return false;
    const response = await app.api.deleteSettingsProfile(profile.id);
    app.commands._refreshSettingsProfilesFromResponse(response);
    app.commands.renderSettingsTab({ preservePendingUi: true });
    app.commands.showToast(`Deleted Settings Profile "${profile.name}"`, "success");
    return true;
  }

  async function handleSettingsProfileSetStartup(profileId) {
    const profile = app.commands.findSettingsProfile(profileId);
    if (!profile) return false;
    if (profile.id === app.state.settings.userDefaultProfileId) {
      return false;
    }
    const response = await app.api.setUserDefaultSettingsProfile(profile.id);
    app.commands._refreshSettingsProfilesFromResponse(response);
    app.commands.renderSettingsTab({ preservePendingUi: true });
    return true;
  }

  async function handleSettingsProfilesBrowse() {
    let selectedProfileId = app.state.settings.loadedProfileRef?.id || app.state.settings.userDefaultProfileId || app.state.ui.SYSTEM_SETTINGS_PROFILE_ID;
    while (true) {
      const result = await app.commands.showSettingsProfileBrowserModal({
        title: "Settings Profiles",
        selectedProfileId,
        onAction: async ({ action, profileId, selectedProfileId: currentSelectedId }) => {
          if (action === "set_default") {
            await app.commands.handleSettingsProfileSetStartup(profileId);
            return { close: false, selectedProfileId: profileId || currentSelectedId };
          }
          if (action === "delete") {
            await app.commands.handleSettingsProfileDelete(profileId);
            return {
              close: false,
              selectedProfileId: app.state.settings.loadedProfileRef?.id || app.state.settings.userDefaultProfileId || app.state.ui.SYSTEM_SETTINGS_PROFILE_ID,
            };
          }
          return null;
        },
      });
      if (!result) return;
      if (result.action === "load_saved_run") {
        await app.commands.openSavedRunsModal("settings");
        return;
      }
      selectedProfileId = result.profileId || selectedProfileId;
      const selected = app.commands.findSettingsProfile(selectedProfileId);
      if (!selected) {
        selectedProfileId = app.state.settings.userDefaultProfileId || app.state.ui.SYSTEM_SETTINGS_PROFILE_ID;
        continue;
      }

      try {
        if (result.action === "load") {
          const actionLabel = app.state.settings.loadedProfileRef?.id === selected.id
            ? "reloading the saved Settings Profile"
            : "loading another Settings Profile";
          const proceed = await app.commands._guardSettingsProfileTransition(actionLabel);
          if (!proceed) continue;
          await app.commands._doLoadSettingsProfile(selected);
          app.commands.showToast(`Loaded Settings Profile "${selected.name}"`, "success");
          return;
        } else if (result.action === "set_default") {
          await app.commands.handleSettingsProfileSetStartup(selected.id);
        } else if (result.action === "delete") {
          await app.commands.handleSettingsProfileDelete(selected.id);
          selectedProfileId = app.state.settings.loadedProfileRef?.id || app.state.settings.userDefaultProfileId || app.state.ui.SYSTEM_SETTINGS_PROFILE_ID;
        } else if (result.action === "restore") {
          await app.commands.handleRestoreSystemSettingsProfile();
          selectedProfileId = app.state.ui.SYSTEM_SETTINGS_PROFILE_ID;
        } else {
          return;
        }
      } catch (e) {
        app.commands.showToast("Failed: " + e.message, "error");
      }
    }
  }

  async function handleSettingsProfileSave() {
    if (!app.commands.isSettingsProfileModified()) {
      app.commands.showToast("No changes to save", "");
      return;
    }

    const current = app.state.settings.loadedProfileRef ? app.commands.findSettingsProfile(app.state.settings.loadedProfileRef.id) || app.state.settings.loadedProfileRef : null;
    const isNamed = current?.kind === "named";
    const isTemporary = current?.kind === "temporary";
    const choice = await app.commands.appChoice(
      isNamed
        ? `Save changes to "${current.name}"?`
        : isTemporary
          ? "This TEMP profile is session-only. Save it as a new named Settings Profile?"
          : "The system default profile cannot be overwritten. Save this draft as a new Settings Profile?",
      isNamed
        ? [
            { label: "Overwrite", value: "overwrite", kind: "primary" },
            { label: "Save As", value: "save_as" },
            { label: "Cancel", value: "cancel" },
          ]
        : [
            { label: "Save As", value: "save_as", kind: "primary" },
            { label: "Cancel", value: "cancel" },
          ],
      { title: "Save Settings Profile" },
    );
    if (!choice || choice === "cancel") return;

    try {
      if (choice === "overwrite") {
        const updated = await app.commands._overwriteLoadedNamedSettingsProfile();
        if (updated) app.commands.showToast(`Settings Profile "${updated.name}" saved`, "success");
        return;
      }
      await app.commands._ensureDraftSavedAsSettingsProfile(isNamed ? current.name : "");
    } catch (e) {
      app.commands.showToast("Save failed: " + e.message, "error");
    }
  }

  async function handleSettingsProfileSaveAs() {
    try {
      await app.commands._ensureDraftSavedAsSettingsProfile(
        app.state.settings.loadedProfileRef?.kind === "named" ? app.state.settings.loadedProfileRef.name : ""
      );
    } catch (e) {
      app.commands.showToast("Save failed: " + e.message, "error");
    }
  }

  async function handleSettingsProfileSetDefault() {
    const current = app.state.settings.loadedProfileRef ? app.commands.findSettingsProfile(app.state.settings.loadedProfileRef.id) || app.state.settings.loadedProfileRef : null;
    if (!current) return;
    if (current.kind === "temporary") {
      app.commands.showToast("Save this TEMP profile as a named Settings Profile before making it the startup default", "warn");
      return;
    }

    try {
      if (!app.commands.isSettingsProfileModified()) {
        if (current.id === app.state.settings.userDefaultProfileId) {
          return;
        }
        const response = await app.api.setUserDefaultSettingsProfile(current.id);
        app.commands._refreshSettingsProfilesFromResponse(response);
        app.commands.renderSettingsTab({ preservePendingUi: true });
        return;
      }

      const created = await app.commands._ensureDraftSavedAsSettingsProfile(
        current.kind === "named" ? current.name : ""
      );
      if (!created) return;
      const makeDefault = await app.commands.appConfirm(
        `Make "${created.name}" the user default Settings Profile?`,
        { ok: "Make Default", cancel: "Not Now" }
      );
      if (!makeDefault) return;
      const response = await app.api.setUserDefaultSettingsProfile(created.id);
      app.commands._refreshSettingsProfilesFromResponse(response);
      app.commands.renderSettingsTab({ preservePendingUi: true });
    } catch (e) {
      app.commands.showToast("Failed: " + e.message, "error");
    }
  }

  async function handleRestoreSystemSettingsProfile() {
    const proceed = await app.commands._guardSettingsProfileTransition("restoring the system default");
    if (!proceed) return;
    try {
      const response = await app.api.restoreSystemSettingsProfile();
      app.commands._refreshSettingsProfilesFromResponse(response);
      const systemProfile = app.commands.findSettingsProfile(app.state.ui.SYSTEM_SETTINGS_PROFILE_ID);
      if (systemProfile) {
        await app.commands._doLoadSettingsProfile(systemProfile);
      }
      app.commands.showToast("System default Settings Profile restored", "success");
    } catch (e) {
      app.commands.showToast("Restore failed: " + e.message, "error");
    }
  }

  function renderPresetBar() {
    app.commands.renderSettingsProfileBar();
  }

  function updateBorderVisibility() {
    // Border is now a toggle switch; CSS :has(.is-on) handles field opacity
    const toggle = app.state.ui.$("#borderToggle");
    const checkbox = app.state.ui.$("#cfgBorder");
    if (toggle && checkbox) {
      toggle.classList.toggle("is-on", checkbox.checked);
    }
  }

  function readSolvePreflightNumber(fieldId, configKey, fallback) {
    const el = typeof app.state.ui.$ === "function" ? app.state.ui.$(`#${fieldId}`) : null;
    const domValue = parseFloat(el?.value);
    if (Number.isFinite(domValue)) return domValue;
    const configValue = parseFloat(app.state.settings.config?.[configKey]);
    return Number.isFinite(configValue) ? configValue : fallback;
  }

  function readSolvePreflightMinCapLayers(layerHeight) {
    const el = typeof app.state.ui.$ === "function" ? app.state.ui.$("#cfgDWcMin") : null;
    const domValue = parseInt(el?.value, 10);
    if (Number.isFinite(domValue) && domValue >= 1) return domValue;
    return app.commands.minCapLayersFromThickness(app.state.settings.config?.d_wc_min, layerHeight);
  }

  function calculateStackLayerAlignment(layerHeight, baseThickness, minCapThickness, maxTotalThickness) {
    const colorBudget = Math.round((maxTotalThickness - baseThickness - minCapThickness) * 1e6) / 1e6;
    const maxLayers = colorBudget > 0 ? Math.floor(colorBudget / layerHeight + 1e-9) : 0;
    const usedBudget = Math.round(maxLayers * layerHeight * 1e6) / 1e6;
    const remainderMm = Math.round((colorBudget - usedBudget) * 1e6) / 1e6;
    return {
      colorBudget,
      maxLayers,
      usedBudget,
      remainderMm,
      lowerTotalMm: baseThickness + minCapThickness + maxLayers * layerHeight,
      upperTotalMm: baseThickness + minCapThickness + (maxLayers + 1) * layerHeight,
    };
  }

  function buildStackLayerAlignmentIssue(alignment) {
    return `${alignment.remainderMm.toFixed(2)} mm of Max Total Thickness cannot be allocated in whole Layer Height steps. `
      + `Set Max Total Thickness to ${alignment.lowerTotalMm.toFixed(2)} mm (${alignment.maxLayers} color layers) `
      + `or ${alignment.upperTotalMm.toFixed(2)} mm (${alignment.maxLayers + 1} color layers)`;
  }

  function buildSolvePitchNozzleIssue(pitch, nozzleSize) {
    const pitchText = Number(pitch).toFixed(3).replace(/\.?0+$/, "");
    const nozzleText = Number(nozzleSize).toFixed(3).replace(/\.?0+$/, "");
    return `Solve Pitch (${pitchText} mm) cannot be smaller than the active nozzle diameter (${nozzleText} mm). `
      + "Increase Solve Pitch or choose a smaller nozzle.";
  }

  function getSolveSettingsPreflightIssues() {
    const lh = app.commands.readSolvePreflightNumber("cfgLayerHeight", "layer_height", 0.08);
    const dwb = app.commands.readSolvePreflightNumber("cfgDWb", "d_wb", 0.20);
    const dwcMinLayers = app.commands.readSolvePreflightMinCapLayers(lh);
    const dwcMin = app.commands.minCapThicknessFromLayers(dwcMinLayers, lh);
    const tMax = app.commands.readSolvePreflightNumber("cfgTMax", "t_max", 2.5);
    const solvePitch = app.commands.readSolvePreflightNumber(
      "cfgSolvePitch",
      "solver_fine_pitch_mm",
      parseFloat(app.state.settings.config?.image_sample_pitch_mm) || 0.20,
    );
    const nozzle = typeof app.state.session.activeNozzle !== "undefined" ? app.state.session.activeNozzle : null;
    const eps = 0.001;
    const pitchEps = 1e-6;
    const issues = [];

    if (nozzle) {
      if (lh < nozzle.min_layer_height - eps) {
        issues.push(`Layer Height (${lh} mm) is below the ${nozzle.size} mm nozzle minimum (${nozzle.min_layer_height} mm)`);
      } else if (lh > nozzle.max_layer_height + eps) {
        issues.push(`Layer Height (${lh} mm) exceeds the ${nozzle.size} mm nozzle maximum (${nozzle.max_layer_height} mm)`);
      }
      const nozzleSize = Number(nozzle.size);
      if (Number.isFinite(nozzleSize) && solvePitch < nozzleSize - pitchEps) {
        issues.push(app.commands.buildSolvePitchNozzleIssue(solvePitch, nozzleSize));
      }
    }

    const alignment = app.commands.calculateStackLayerAlignment(lh, dwb, dwcMin, tMax);
    if (alignment.colorBudget <= 0) {
      issues.push("No color space — base + cap exceed max total thickness");
      return issues;
    }

    if (alignment.remainderMm > eps) {
      issues.push(app.commands.buildStackLayerAlignmentIssue(alignment));
    }
    return issues;
  }

  function buildSolveSettingsPreflightMessage(issues) {
    return `Can't solve. Fix settings: ${(issues || []).join(" ")}`.trim();
  }

  function updateDerivedParams() {
    const lh = parseFloat(app.state.ui.$("#cfgLayerHeight").value) || 0.08;
    const dwb = parseFloat(app.state.ui.$("#cfgDWb").value) || 0.20;
    const dwcMinEl = app.state.ui.$("#cfgDWcMin");
    let dwcMinLayers = parseInt(dwcMinEl?.value, 10);
    if (!Number.isFinite(dwcMinLayers) || dwcMinLayers < 1) {
      dwcMinLayers = 1;
      if (dwcMinEl) dwcMinEl.value = "1";
    }
    const dwcMin = app.commands.minCapThicknessFromLayers(dwcMinLayers, lh);
    const tMax = parseFloat(app.state.ui.$("#cfgTMax").value) || 2.5;

    // Update layer height range label from active nozzle
    const lhLabel = app.state.ui.$("#cfgLayerHeight")?.closest("tr")?.querySelector(".stg-range");
    if (lhLabel) {
      const lo = app.state.session.activeNozzle?.min_layer_height ?? 0.04;
      const hi = app.state.session.activeNozzle?.max_layer_height ?? 0.20;
      lhLabel.textContent = `${lo}\u2013${hi}`;
    }
    const solvePitchHint = app.state.ui.$("#cfgSolvePitchHint");
    if (solvePitchHint) {
      const minimumPitch = Number(app.state.session.activeNozzle?.size ?? 0.20);
      const pitchText = Number.isFinite(minimumPitch)
        ? minimumPitch.toFixed(2).replace(/0+$/, "").replace(/\.$/, "")
        : "0.2";
      solvePitchHint.textContent = `minimum ${pitchText} mm`;
    }

    const alignment = app.commands.calculateStackLayerAlignment(lh, dwb, dwcMin, tMax);
    const { colorBudget, maxLayers, usedBudget, remainderMm } = alignment;
    const eps = 0.001;
    const el = app.state.ui.$("#derivedParams");
    const footnote = app.state.ui.$("#stackDerived");

    if (colorBudget <= 0) {
      if (footnote) footnote.innerHTML = `<div class="stg-warn">No color space — base + cap exceed max total thickness</div>`;
      el.innerHTML = "";
      return;
    }

    // Stack geometry footnote — keep it clean, one line each
    if (footnote) {
      footnote.innerHTML = `
        <div>Color layers: <strong>${maxLayers}</strong> at ${lh} mm = <strong>${usedBudget.toFixed(2)} mm</strong></div>
      `;
    }

    // Validation warnings
    const warnings = [];

    // Nozzle compatibility
    if (app.state.session.activeNozzle) {
      if (lh < app.state.session.activeNozzle.min_layer_height - eps) {
        warnings.push(`Layer Height (${lh} mm) is below the ${app.state.session.activeNozzle.size} mm nozzle minimum (${app.state.session.activeNozzle.min_layer_height} mm)`);
      } else if (lh > app.state.session.activeNozzle.max_layer_height + eps) {
        warnings.push(`Layer Height (${lh} mm) exceeds the ${app.state.session.activeNozzle.size} mm nozzle maximum (${app.state.session.activeNozzle.max_layer_height} mm)`);
      }
      const solvePitch = app.commands.getCurrentSolvePitch();
      const nozzleSize = Number(app.state.session.activeNozzle.size);
      if (Number.isFinite(nozzleSize) && solvePitch < nozzleSize - 1e-6) {
        warnings.push(app.commands.buildSolvePitchNozzleIssue(solvePitch, nozzleSize));
      }
    }

    // Divisibility checks
    if (remainderMm > eps) {
      warnings.push(app.commands.buildStackLayerAlignmentIssue(alignment));
    }
    // Soft range hints (not printability issues, just gentle nudges)
    const hints = [];
    const rangeChecks = [
      { id: "cfgLayerHeight", val: lh, min: app.state.session.activeNozzle?.min_layer_height ?? 0.04, max: app.state.session.activeNozzle?.max_layer_height ?? 0.20, name: "Layer Height" },
      { id: "cfgDWb", val: dwb, min: 0.08, max: 1.0, name: "Base Thickness" },
      { id: "cfgDWcMin", val: dwcMin, min: lh, max: 0.50, name: "Min Cap Thickness" },
      { id: "cfgTMax", val: tMax, min: 1.0, max: 5.0, name: "Max Total Thickness" },
      { id: "cfgKMax", val: parseInt(app.state.ui.$("#cfgKMax")?.value) || 3, min: 1, max: 7, name: "Max Colors per Region" },
      { id: "cfgDeThreshold", val: parseFloat(app.state.ui.$("#cfgDeThreshold")?.value) || 0.01, min: 0.01, max: 0.20, name: "Color Mismatch Tolerance" },
      { id: "cfgSmoothKernel", val: Number.isNaN(parseFloat(app.state.ui.$("#cfgSmoothKernel")?.value)) ? app.commands.smoothingRadiusMmFromCells(app.state.settings.config.smooth_kernel) : parseFloat(app.state.ui.$("#cfgSmoothKernel")?.value), min: 0, max: 20, name: "Smoothing Radius" },
    ];
    for (const { val, min, max, name } of rangeChecks) {
      if (val < min - eps) hints.push(`${name} (${val}) is below the suggested minimum of ${min}`);
      else if (val > max + eps) hints.push(`${name} (${val}) is above the suggested maximum of ${max}`);
    }

    // Clear all field-level warning indicators
    document.querySelectorAll(".stg-field-warn").forEach(el => el.remove());

    // Add ⚠ glyph next to fields that triggered warnings
    const warnFieldIds = new Set();
    if (app.state.session.activeNozzle && (lh < app.state.session.activeNozzle.min_layer_height - eps || lh > app.state.session.activeNozzle.max_layer_height + eps)) {
      warnFieldIds.add("cfgLayerHeight");
    }
    if (remainderMm > eps) {
      warnFieldIds.add("cfgLayerHeight");
      warnFieldIds.add("cfgTMax");
    }
    if (app.state.session.activeNozzle && app.commands.getCurrentSolvePitch() < Number(app.state.session.activeNozzle.size) - 1e-6) {
      warnFieldIds.add("cfgSolvePitch");
    }

    // Border height must be >= base thickness (d_wb)
    if (app.state.settings.config.border) {
      const borderHeightVal = parseFloat(app.state.ui.$("#cfgBorderHeight")?.value) || 0;
      if (borderHeightVal < dwb - eps) {
        warnings.push(`Border Height (${borderHeightVal} mm) must be at least the base thickness (${dwb} mm)`);
        warnFieldIds.add("cfgBorderHeight");
      }
    }

    for (const id of warnFieldIds) {
      const input = app.state.ui.$(`#${id}`);
      if (input) {
        const wrapper = input.closest(".input-with-unit");
        if (wrapper && !wrapper.querySelector(".stg-field-warn")) {
          const mark = document.createElement("span");
          mark.className = "stg-field-warn";
          mark.textContent = "\u26a0";
          wrapper.parentElement.insertBefore(mark, wrapper);
        }
      }
    }

    let html = "";
    if (warnings.length) {
      html += warnings.map(w => `<div class="stg-warn">\u26a0 ${w}</div>`).join("");
    }
    if (hints.length) {
      html += hints.map(h => `<div class="stg-hint">${h}</div>`).join("");
    }
    el.innerHTML = html;
  }

  function applyDraftNumberField(key, rawValue, {
    parse = parseFloat,
    isValid = (value) => !Number.isNaN(value),
  } = {}) {
    const value = parse(rawValue);
    if (!isValid(value)) return false;
    app.state.settings.config[key] = value;
    return true;
  }

  function bindDraftNumberInput(id, applyDraft) {
    const el = app.state.ui.$(`#${id}`);
    if (!el) return;
    el.addEventListener("input", () => {
      applyDraft(el.value, el);
    });
  }

  function readBoundedNumberInput(id, fallback, options = {}) {
    const el = app.state.ui.$(`#${id}`);
    if (!el) return fallback;
    const coerced = app.commands.coerceNumberValue(el.value, fallback, options);
    if (coerced.ok) el.value = coerced.value;
    return coerced.value;
  }

  function readOptionalNumberInput(id, options = {}) {
    const el = app.state.ui.$(`#${id}`);
    if (!el) return null;
    const raw = String(el.value || "").trim();
    if (!raw) return null;
    const coerced = app.commands.coerceNumberValue(raw, null, options);
    if (coerced.ok) {
      el.value = coerced.value;
      return coerced.value;
    }
    el.value = "";
    return null;
  }

  function setOptionalNumberInput(id, value) {
    const el = app.state.ui.$(`#${id}`);
    if (!el) return;
    el.value = value === null || value === undefined ? "" : value;
  }

  function readConfigFromUI() {
    app.state.settings.config.base_filament = app.state.ui.$("#cfgBaseFilament")?.value || app.state.session.DEFAULT_BASE_FILAMENT;
    app.state.settings.config.cap_filament = "__same__";
    app.state.settings.config.layer_height = app.commands.readBoundedNumberInput("cfgLayerHeight", app.state.settings.config.layer_height, { min: 0.001 });
    app.state.settings.config.d_wb = app.commands.readBoundedNumberInput("cfgDWb", app.state.settings.config.d_wb, { min: 0.001 });
    {
      const capLayers = app.commands.readBoundedNumberInput(
        "cfgDWcMin",
        app.commands.minCapLayersFromThickness(app.state.settings.config.d_wc_min, app.state.settings.config.layer_height),
        { parse: (value) => parseInt(value, 10), min: 1, integer: true },
      );
      app.state.settings.config.d_wc_min = app.commands.minCapThicknessFromLayers(capLayers, app.state.settings.config.layer_height);
    }
    app.state.settings.config.t_max = app.commands.readBoundedNumberInput("cfgTMax", app.state.settings.config.t_max, { min: 0.001 });
    app.state.settings.config.k_max = app.commands.readBoundedNumberInput("cfgKMax", app.state.settings.config.k_max, { parse: (value) => parseInt(value, 10), min: 1, max: 7, integer: true });
    app.state.settings.config.de_threshold = app.commands.readBoundedNumberInput("cfgDeThreshold", app.state.settings.config.de_threshold, { min: 0 });
    app.state.settings.config.gamut_mode = app.commands.normalizeActiveGamutMode(app.state.ui.$("#cfgGamutMode")?.value || app.state.settings.config.gamut_mode || "hull");
    app.state.settings.config.gamut_white_rescale = app.state.ui.$("#cfgGamutWhiteRescale")?.checked ?? app.state.settings.config.gamut_white_rescale;
    app.state.settings.config.model_domain_ingress = true;
    {
      const radiusMm = app.commands.readBoundedNumberInput(
        "cfgSmoothKernel",
        app.commands.smoothingRadiusMmFromCells(app.state.settings.config.smooth_kernel),
        { min: 0 },
      );
      app.state.settings.config.smooth_kernel = app.commands.smoothingCellsFromRadiusMm(radiusMm, app.commands.getCurrentSolvePitch());
    }
    app.commands.syncChromaWeightControlFromConfig();
    app.state.settings.config.source_resample_kernel = app.state.ui.$("#cfgSourceResampleKernel")?.value || app.state.settings.config.source_resample_kernel || "lanczos";
    app.state.settings.config.appearance_model_provider = app.state.ui.$("#cfgAppearanceModelProvider")?.value || app.state.settings.config.appearance_model_provider || "photo_stack_bundle";
    if (app.state.settings.config.appearance_model_provider !== "photo_stack_bundle") {
      app.state.settings.config.photo_stack_bundle_path = null;
    }
    // Module params are written directly to config by renderParamRow's input/change
    // handlers — no need to read from HTML here. Static settings that still have
    // hardcoded HTML elements:
    app.state.settings.config.border = app.state.ui.$("#cfgBorder")?.checked ?? app.state.settings.config.border;
    app.state.settings.config.border_width_mm = app.commands.readBoundedNumberInput("cfgBorderWidth", app.state.settings.config.border_width_mm, { min: 0 });
    app.state.settings.config.border_height_mm = app.commands.readBoundedNumberInput("cfgBorderHeight", app.state.settings.config.border_height_mm, { min: 0 });
    app.state.settings.config.use_corrections = app.state.ui.$("#cfgUseCorrections")?.checked ?? app.state.settings.config.use_corrections;
    const solvePitch = app.commands.getCurrentSolvePitch();
    app.state.settings.config.image_sample_pitch_mm = solvePitch;
    app.state.settings.config.solver_fine_pitch_mm = solvePitch;
    const selectedLuminanceMode = app.commands.normalizeLuminanceMode(app.commands.getSolveModeControlValue());
    const selectedCapMode = app.state.ui.$("#cfgCapMode")?.value || app.state.settings.config.cap_mode || "appearance_bounded_smooth";
    if (selectedLuminanceMode !== "luminance_detail") {
      app.state.settings.config.cap_mode = selectedCapMode;
      app.commands.saveLastColorCapMode(app.state.settings.config.cap_mode);
      app.state.settings.capModeForcedByLuminance = false;
    }
    if (selectedLuminanceMode === "luminance_detail") {
      app.state.settings.config.cap_mode = "smooth_variable";
      const capModeEl = app.state.ui.$("#cfgCapMode");
      if (capModeEl) capModeEl.value = "smooth_variable";
    }
    app.state.settings.config.boundary_cap_de_budget = app.commands.readBoundedNumberInput("cfgBoundaryCapDeBudget", app.state.settings.config.boundary_cap_de_budget ?? 0.008, { min: 0 });
    app.state.settings.config.detail_cap_enabled = true;
    {
      const detailLayerRaw = String(app.state.ui.$("#cfgDetailCapMaxLayers")?.value || "").trim();
      const detailMaxLayers = /^[0-9]+$/.test(detailLayerRaw)
        ? parseInt(detailLayerRaw, 10)
        : NaN;
      app.state.settings.config.detail_cap_max_layers = Number.isFinite(detailMaxLayers)
        ? Math.max(0, detailMaxLayers)
        : (app.state.settings.config.detail_cap_max_layers ?? 5);
    }
    app.commands.applyMandatoryProductSettings();
    app.state.settings.config.cell_mode = app.state.ui.$("#cfgCellMode")?.value || app.state.settings.config.cell_mode || "felzenszwalb";
    app.state.settings.config.stage1_coarsening_factor = app.commands.readBoundedNumberInput("cfgStage1Coarsening", app.state.settings.config.stage1_coarsening_factor || 1, { parse: (value) => parseInt(value, 10), min: 1, max: 4, integer: true });
    app.state.settings.config.color_region_target_mm = app.commands.readBoundedNumberInput("cfgColorRegionTarget", app.state.settings.config.color_region_target_mm, { min: 0.001 });
    // Blueprint printability diagnostics stay on for normal app solves.
    // Heavier pressure/geometry attribution artifacts are research-only and
    // can still be enabled from scripts or API payloads.
    app.state.settings.config.emit_pressure_diagnostics = false;
    app.state.settings.config.emit_geometry_attribution = false;
    app.state.settings.config.emit_blueprint_printability = true;
    app.state.settings.config.printability_minimum_extrusion_width_mm = app.state.session.activeNozzle?.min_line_width ?? null;
    app.state.settings.config.printability_minimum_line_length_mm = app.state.session.activeNozzle?.min_line_length ?? null;
    // Product printability enforcement is mandatory. Width multiplier remains
    // an internal/profile value and keeps whatever was loaded.
    app.state.settings.config.color_region_target_from_printability = true;
    app.state.settings.config.stage2_fine_override_enabled = app.state.ui.$("#cfgStage2FineOverride")?.checked ?? app.state.settings.config.stage2_fine_override_enabled;
    app.state.settings.config.stage2_boundary_mutation_enabled = app.state.ui.$("#cfgStage2BoundaryMutation")?.checked ?? app.state.settings.config.stage2_boundary_mutation_enabled;
    app.state.settings.config.stage2_boundary_mutation_current_de_percentile = app.commands.readOptionalNumberInput("cfgStage2BoundaryMutationPercentile", { min: 0, max: 100 });
    app.state.settings.config.stage2_boundary_mutation_max_passes = app.commands.readOptionalNumberInput("cfgStage2BoundaryMutationMaxPasses", { min: 1, max: 16 }) ?? 1;
    app.state.settings.config.stage2_boundary_mutation_min_gain = app.commands.readOptionalNumberInput("cfgStage2BoundaryMutationMinGain", { min: 0 });
    app.state.settings.config.stage2_boundary_mutation_min_component_mm = app.commands.readOptionalNumberInput("cfgStage2BoundaryMutationMinComponent", { min: 0 });
    const baseShadingLimitEl = app.commands.getBaseShadingLimitInput();
    if (baseShadingLimitEl) {
      const fraction = app.commands.setLuminanceBaseShadingLimitFraction(
        app.commands.parseLuminanceBaseShadingLimitPercent(baseShadingLimitEl.value),
      );
      app.commands.syncBaseShadingLimitControls(app.commands.formatLuminanceBaseShadingLimitPercent(fraction));
    }
    app.state.settings.config.luminance_mode = app.commands.applyLuminanceMode(selectedLuminanceMode, { resetStandard: true });
    app.state.settings.config.swap_improvement_threshold = app.commands.readBoundedNumberInput("paletteSwapThreshold", app.state.settings.config.swap_improvement_threshold || 2.0, { min: 0 });
    app.state.settings.config.force_all_tiers = app.state.ui.$("#paletteForceAllTiers")?.checked || false;
  }

  function _formatConfigSyncError(err) {
    if (!err) return "unknown error";
    if (typeof err.message === "string" && err.message.trim()) return err.message.trim();
    return String(err);
  }

  async function syncConfigToServer({ throwOnError = false, showErrorToast = false } = {}) {
    if (!app.state.session.apiConnected) return;
    app.commands.syncConfigFromModuleState();
    app.commands.readConfigFromUI();
    // Serialize config writes so older requests cannot land after newer ones
    // and revert the session immediately before solve start.
    const frame = app.state.image.selectedImage ? {
      width_mm: app.state.image.frameState.widthMm,
      height_mm: app.state.image.frameState.heightMm,
      scale: app.state.image.frameState.scale,
      rotation: app.state.image.frameState.rotation,
      pan_x: app.state.image.frameState.panX,
      pan_y: app.state.image.frameState.panY,
      flip_h: app.state.image.frameState.flipH,
      flip_v: app.state.image.frameState.flipV,
    } : null;
    const payload = {
      ...app.state.settings.config,
      image_path: app.state.image.selectedImage?.filename || null,
      palette: app.commands.getActivePalette(),
      white_base: app.commands.getBaseFilament(),
      white_cap: null,
      ams_slots: app.state.session.printerConfig.ams_slots,
      white_slots: app.state.session.printerConfig.white_slots,
      frame,
      image_adjust: app.state.image.imageAdjust,
    };
    const runSync = async () => {
      const response = await app.api.updateConfig(payload);
      if (response?.config) {
        Object.assign(app.state.settings.config, response.config);
      }
      return response;
    };
    const pendingSync = app.state.settings._configSyncChain.catch(() => {}).then(runSync);
    app.state.settings._configSyncChain = pendingSync.catch(() => {});
    try {
      return await pendingSync;
    } catch (err) {
      console.warn("[config] sync failed:", err.message);
      if (showErrorToast) {
        app.commands.showToast(`Couldn't sync settings to the server: ${app.commands._formatConfigSyncError(err)}`, "error");
      }
      if (throwOnError) throw err;
    }
  }

  function renderSolveTab() {
    app.commands.renderSolveRunSidebar();
    app.commands.renderSolveProgress();
    app.commands.renderSolveComparisonGrid();

    app.commands.updateSolveReadiness();
  }

  function getCurrentSolvePitch() {
    return parseFloat(app.state.ui.$("#cfgSolvePitch")?.value)
      || app.state.settings.config.image_sample_pitch_mm
      || 0.20;
  }

  function applySolvePitchDraft(rawValue, mirrorEl = null) {
    const previousPitch = app.commands.getCurrentSolvePitch();
    const radiusMm = app.commands.smoothingRadiusMmFromCells(app.state.settings.config.smooth_kernel, previousPitch);
    const v = parseFloat(rawValue);
    if (!(v > 0)) return false;
    app.state.settings.config.image_sample_pitch_mm = v;
    app.state.settings.config.solver_fine_pitch_mm = v;
    app.state.settings.config.smooth_kernel = app.commands.smoothingCellsFromRadiusMm(radiusMm, v);
    const smoothEl = app.state.ui.$("#cfgSmoothKernel");
    if (smoothEl) smoothEl.value = radiusMm;
    if (mirrorEl && mirrorEl.value !== rawValue) mirrorEl.value = rawValue;
    app.commands.updateInfoGrid();
    app.commands.renderPreview();
    return true;
  }

  function updateSolveReadiness() {
    const btn = app.state.ui.$("#startSolveBtn");
    if (!btn) return;
    const canSolve = app.state.session.apiConnected && app.state.image.selectedImage && app.commands.getActivePalette().length > 0;
    const isRunning = app.state.solve.solveStatus.status === "running";
    btn.disabled = !(canSolve && !app.state.solve.solveStartPending && !isRunning && !app.state.export.exportRunning);
    btn.title = app.state.export.exportRunning
      ? "Please wait for meshing to finish"
      : app.state.solve.solveStartPending
        ? "Starting solve..."
        : isRunning
          ? "A solve is already running"
          : "Solve the active palette";
  }

  Object.assign(app.commands, {
    _configSettingsProfileSnapshot,
    _cloneValue,
    _dropRetiredSettingsProfileKeys,
    _normalizeSettingsProfileModules,
    _currentSettingsProfileModulesSnapshot,
    _settingsProfileModulesEqual,
    _settingsProfileValuesEqual,
    _applyModuleSnapshot,
    _applySettingsProfileToConfig,
    _setLoadedSettingsProfile,
    allSettingsProfiles,
    findSettingsProfile,
    isSettingsProfileModified,
    findSettingsProfileByName,
    _settingsProfileBadges,
    _settingsProfileBadgesHtml,
    describeSettingsProfileNameInput,
    _refreshSettingsProfilesFromResponse,
    validateSettingsProfileNameLocal,
    _captureLiveSettingsProfileState,
    _restoreLiveSettingsProfileState,
    _runSettingsMetadata,
    _settingsSnapshotFromRunPayload,
    _modulesSnapshotFromRunPayload,
    buildTemporarySettingsProfileFromRun,
    _doLoadSettingsProfile,
    _loadTemporarySettingsFromRun,
    loadSettingsProfiles,
    loadPresets,
    renderSettingsProfileBar,
    checkPresetModified,
    _renderSettingsProfileList,
    _settingsProfileSelectionHtml,
    showSettingsProfileBrowserModal,
    showSettingsProfileSaveAsModal,
    _guardSettingsProfileTransition,
    _saveDraftAsSettingsProfileWithName,
    _overwriteSettingsProfile,
    _overwriteLoadedNamedSettingsProfile,
    _ensureDraftSavedAsSettingsProfile,
    handleSettingsProfileDelete,
    handleSettingsProfileSetStartup,
    handleSettingsProfilesBrowse,
    handleSettingsProfileSave,
    handleSettingsProfileSaveAs,
    handleSettingsProfileSetDefault,
    handleRestoreSystemSettingsProfile,
    renderPresetBar,
    updateBorderVisibility,
    readSolvePreflightNumber,
    readSolvePreflightMinCapLayers,
    calculateStackLayerAlignment,
    buildStackLayerAlignmentIssue,
    buildSolvePitchNozzleIssue,
    getSolveSettingsPreflightIssues,
    buildSolveSettingsPreflightMessage,
    updateDerivedParams,
    applyDraftNumberField,
    bindDraftNumberInput,
    readBoundedNumberInput,
    readOptionalNumberInput,
    setOptionalNumberInput,
    readConfigFromUI,
    _formatConfigSyncError,
    syncConfigToServer,
    renderSolveTab,
    getCurrentSolvePitch,
    applySolvePitchDraft,
    updateSolveReadiness,
  });}
