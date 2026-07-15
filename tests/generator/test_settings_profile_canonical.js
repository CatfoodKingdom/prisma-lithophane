const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP_JS = path.resolve(__dirname, '../../Prisma/generator/app/app.js');
const SOURCE = fs.readFileSync(APP_JS, 'utf8');

function extractFunction(signature) {
  const start = SOURCE.indexOf(signature);
  if (start === -1) {
    throw new Error(`Could not find function signature: ${signature}`);
  }
  const closeParams = SOURCE.indexOf(')', start);
  if (closeParams === -1) {
    throw new Error(`Could not find closing paren for: ${signature}`);
  }
  const openBrace = SOURCE.indexOf('{', closeParams);
  if (openBrace === -1) {
    throw new Error(`Could not find opening brace for: ${signature}`);
  }
  let depth = 0;
  for (let i = openBrace; i < SOURCE.length; i += 1) {
    const ch = SOURCE[i];
    if (ch === '{') depth += 1;
    if (ch === '}') {
      depth -= 1;
      if (depth === 0) return SOURCE.slice(start, i + 1);
    }
  }
  throw new Error(`Could not find closing brace for: ${signature}`);
}

function extractBracketedValue(signature, openChar, closeChar) {
  const start = SOURCE.indexOf(signature);
  if (start === -1) {
    throw new Error(`Could not find signature: ${signature}`);
  }
  const open = SOURCE.indexOf(openChar, start);
  if (open === -1) {
    throw new Error(`Could not find opening ${openChar} for: ${signature}`);
  }
  let depth = 0;
  for (let i = open; i < SOURCE.length; i += 1) {
    const ch = SOURCE[i];
    if (ch === openChar) depth += 1;
    if (ch === closeChar) {
      depth -= 1;
      if (depth === 0) return SOURCE.slice(open, i + 1);
    }
  }
  throw new Error(`Could not find closing ${closeChar} for: ${signature}`);
}

function loadAppProfileKeys() {
  return vm.runInNewContext(
    `(${extractBracketedValue('const SETTINGS_PROFILE_KEYS =', '[', ']')})`,
    {},
    { filename: APP_JS },
  );
}

function loadInitialConfigKeys() {
  const context = {};
  const configObject = extractBracketedValue('let config =', '{', '}');
  vm.runInNewContext(`config = ${configObject};`, context, { filename: APP_JS });
  return Object.keys(context.config);
}

function loadModulePosture() {
  return vm.runInNewContext(
    `(${extractBracketedValue('const MODULE_POSTURE =', '{', '}')})`,
    {},
    { filename: APP_JS },
  );
}

function loadCanonicalProfileFunctions() {
  const context = {
    console,
    SETTINGS_PROFILE_KEYS: [
      'image_sample_pitch_mm',
      'solver_fine_pitch_mm',
      'color_region_target_mm',
    ],
    config: {
      image_sample_pitch_mm: 0.25,
      solver_fine_pitch_mm: 0.25,
      color_region_target_mm: 1.25,
    },
    SETTINGS_PROFILE_DEFAULTS: {
      image_sample_pitch_mm: 0.25,
      solver_fine_pitch_mm: 0.25,
      color_region_target_mm: 1.25,
    },
    moduleData: [
      { name: 'a1_bilateral_denoise', slot: 'preprocessing', default_enabled: false },
    ],
  };

  const script = [
    extractFunction('function _cloneValue'),
    extractFunction('function _dropRetiredSettingsProfileKeys'),
    extractFunction('function _normalizeSettingsProfileModules'),
    extractFunction('function _applySettingsProfileToConfig'),
  ].join('\n\n');
  vm.runInNewContext(script, context, { filename: APP_JS });
  return context;
}

function loadNestedProfileFunctions() {
  const context = {
    console,
    SETTINGS_PROFILE_KEYS: [
      'preprocessing_params',
    ],
    config: {
      preprocessing_params: {},
    },
    SETTINGS_PROFILE_DEFAULTS: {
      preprocessing_params: {},
    },
    loadedProfileSnapshot: null,
    syncConfigFromModuleState() {},
    readConfigFromUI() {},
    _currentSettingsProfileModulesSnapshot() {
      return {};
    },
    _settingsProfileModulesEqual() {
      return true;
    },
  };

  const script = [
    extractFunction('function _cloneValue'),
    extractFunction('function _dropRetiredSettingsProfileKeys'),
    extractFunction('function applyMandatoryProductSettings'),
    extractFunction('function _currentSettingsSnapshot'),
    extractFunction('function _settingsProfileValuesEqual'),
    extractFunction('function _applySettingsProfileToConfig'),
    extractFunction('function isSettingsProfileModified'),
  ].join('\n\n');
  vm.runInNewContext(script, context, { filename: APP_JS });
  return context;
}

function loadProfileLoadFlowFunctions() {
  const context = {
    console,
    SETTINGS_PROFILE_KEYS: [
      'layer_height',
      't_max',
      'gamut_mode',
      'luminance_mode',
      'luminance_detail_authoring_printability',
      'stage2_boundary_mutation_enabled',
      'stage2_boundary_mutation_max_passes',
    ],
    SETTINGS_PROFILE_DEFAULTS: {
      layer_height: 0.08,
      t_max: 3.0,
      gamut_mode: 'hull',
      luminance_mode: 'standard',
      luminance_detail_authoring_printability: 'off',
      stage2_boundary_mutation_enabled: true,
      stage2_boundary_mutation_max_passes: 1,
    },
    config: {
      layer_height: 0.08,
      t_max: 3.0,
      gamut_mode: 'hull',
      luminance_mode: 'standard',
      luminance_detail_authoring_printability: 'off',
      stage2_boundary_mutation_enabled: true,
      stage2_boundary_mutation_max_passes: 1,
    },
    moduleData: [],
    moduleState: {},
    loadedProfileRef: null,
    loadedProfileSnapshot: null,
    temporarySettingsProfile: null,
    apiConnected: false,
    _configSyncChain: Promise.resolve(),
    syncConfigFromModuleState() {},
    readConfigFromUI() {},
    renderSettingsProfileBar() {},
    renderModulePanel() {},
    renderDynamicSettings() {},
    refreshModuleDrivenViews() {},
    async setModuleState(state) { return { state }; },
    async syncConfigToServer() {},
  };
  context.renderSettingsTab = () => {
    context.domTMax = context.config.t_max;
    if (context.config.luminance_mode === 'luminance_detail') {
      context.config.luminance_detail_authoring_printability = 'absolute_finalgate';
    }
  };

  const script = [
    extractFunction('function _cloneValue'),
    extractFunction('function _dropRetiredSettingsProfileKeys'),
    extractFunction('function applyMandatoryProductSettings'),
    extractFunction('function _normalizeSettingsProfileModules'),
    extractFunction('function _currentSettingsProfileModulesSnapshot'),
    extractFunction('function _settingsProfileModulesEqual'),
    extractFunction('function _settingsProfileValuesEqual'),
    extractFunction('function normalizeActiveGamutMode'),
    extractFunction('function _configSettingsProfileSnapshot'),
    extractFunction('function _currentSettingsSnapshot'),
    extractFunction('async function _applyModuleSnapshot'),
    extractFunction('function _applySettingsProfileToConfig'),
    extractFunction('function _setLoadedSettingsProfile'),
    extractFunction('function _captureLiveSettingsProfileState'),
    extractFunction('async function _restoreLiveSettingsProfileState'),
    extractFunction('function isSettingsProfileModified'),
    extractFunction('async function _doLoadSettingsProfile'),
  ].join('\n\n');
  vm.runInNewContext(script, context, { filename: APP_JS });
  return context;
}

function loadTemporaryProfileFunctions() {
  const context = {
    SETTINGS_PROFILE_KEYS: ['layer_height', 'gamut_mode', 'preprocessing_params'],
    SETTINGS_PROFILE_DEFAULTS: {
      layer_height: 0.08,
      gamut_mode: 'hull',
      preprocessing_params: {},
    },
    moduleData: [
      { name: 'a1_bilateral_denoise', slot: 'preprocessing', default_enabled: false },
      { name: 'b1_printscale_bilateral', slot: 'preprocessing', default_enabled: true },
    ],
  };
  const script = [
    extractFunction('function _cloneValue'),
    extractFunction('function _dropRetiredSettingsProfileKeys'),
    extractFunction('function _normalizeSettingsProfileModules'),
    extractFunction('function _runSettingsMetadata'),
    extractFunction('function _settingsSnapshotFromRunPayload'),
    extractFunction('function _modulesSnapshotFromRunPayload'),
    extractFunction('function buildTemporarySettingsProfileFromRun'),
  ].join('\n\n');
  vm.runInNewContext(script, context, { filename: APP_JS });
  return context;
}

function loadLuminanceModeFunctions() {
  const context = {
    config: {
      luminance_mode: 'standard',
      luminance_handler_enabled: false,
      luminance_handler_mode: 'boundary_prior',
      luminance_handler_strength: 1.0,
      luminance_handler_optical_authority_fraction: 0.75,
      luminance_base_shading_limit_fraction: 0.75,
      luminance_handler_boundary_percentile: 95.0,
      luminance_handler_boundary_sigma_px: null,
      luminance_handler_response_curve: 'linear',
      luminance_handler_response_gamma: 1.0,
      luminance_handler_detail_residual: true,
      luminance_handler_include_solver_detail: true,
      detail_cap_enabled: true,
      luminance_detail_authoring_printability: 'off',
      enforce_printability: false,
      emit_blueprint_printability: true,
      cap_continuity_cleanup: true,
      stage2_boundary_mutation_enabled: false,
    },
  };

  const script = [
    extractFunction('function normalizeLuminanceMode'),
    extractFunction('function clampLuminanceBaseShadingLimitFraction'),
    extractFunction('function getLuminanceBaseShadingLimitFraction'),
    extractFunction('function setLuminanceBaseShadingLimitFraction'),
    extractFunction('function formatLuminanceBaseShadingLimitPercent'),
    extractFunction('function applyMandatoryProductSettings'),
    extractFunction('function applyLuminanceMode'),
    extractFunction('function formatLuminanceMode'),
  ].join('\n\n');
  vm.runInNewContext(script, context, { filename: APP_JS });
  return context;
}

function loadGamutModeProfileFunctions() {
  const context = {
    console,
    SETTINGS_PROFILE_KEYS: [
      'gamut_mode',
    ],
    config: {
      gamut_mode: 'hull',
    },
    SETTINGS_PROFILE_DEFAULTS: {
      gamut_mode: 'hull',
    },
  };

  const script = [
    extractFunction('function _cloneValue'),
    extractFunction('function _dropRetiredSettingsProfileKeys'),
    extractFunction('function normalizeActiveGamutMode'),
    extractFunction('function _applySettingsProfileToConfig'),
    extractFunction('function formatGamutMode'),
  ].join('\n\n');
  vm.runInNewContext(script, context, { filename: APP_JS });
  return context;
}

function loadChromaWeightControlFunctions() {
  const elements = {
    cfgChromaWeight: { value: '0' },
    cfgChromaWeightReadout: { textContent: '' },
  };
  const context = {
    console,
    Number,
    Math,
    SETTINGS_PROFILE_KEYS: [
      'chroma_weight',
    ],
    SETTINGS_PROFILE_DEFAULTS: {
      chroma_weight: 1.0,
    },
    config: {
      chroma_weight: 1.0,
    },
    loadedProfileSnapshot: null,
    moduleData: [],
    $: (selector) => {
      if (!selector.startsWith('#')) return null;
      return elements[selector.slice(1)] || null;
    },
    elements,
    applyMandatoryProductSettings() {},
    _currentSettingsProfileModulesSnapshot() {
      return {};
    },
    _settingsProfileModulesEqual() {
      return true;
    },
  };

  const script = [
    'const CHROMA_WEIGHT_SLIDER_MIN = -3;',
    'const CHROMA_WEIGHT_SLIDER_MAX = 3;',
    extractFunction('function _cloneValue'),
    extractFunction('function normalizeChromaWeight'),
    extractFunction('function chromaWeightToSliderPosition'),
    extractFunction('function chromaWeightFromSliderPosition'),
    extractFunction('function formatChromaWeightReadout'),
    extractFunction('function syncChromaWeightControlFromConfig'),
    extractFunction('function applyChromaWeightSliderInput'),
    extractFunction('function _settingsProfileValuesEqual'),
    extractFunction('function _currentSettingsSnapshot'),
    extractFunction('function isSettingsProfileModified'),
    'function readConfigFromUI() { syncChromaWeightControlFromConfig(); }',
  ].join('\n\n');
  vm.runInNewContext(script, context, { filename: APP_JS });
  return context;
}

function loadModuleParamFunctions() {
  const context = {
    moduleData: [
      {
        name: 'b1_printscale_bilateral',
        slot: 'preprocessing',
        params: {
          feature_scale_multiplier: {
            name: 'feature_scale_multiplier',
            default: 1.0,
          },
          sigma_range: {
            name: 'sigma_range',
            default: 0.05,
          },
          passes: {
            name: 'passes',
            default: 1,
          },
        },
      },
    ],
    config: {
      preprocessing_params: {
        b1_printscale_bilateral: {
          sigma_range: 0.035,
        },
      },
    },
  };

  const script = [
    extractFunction('function coerceNumberValue'),
    extractFunction('function coerceNumericParamValue'),
    extractFunction('function moduleParamStorageKey'),
    extractFunction('function moduleDescriptorById'),
    extractFunction('function getModuleParamValue'),
    extractFunction('function setModuleParamValue'),
    extractFunction('function projectModuleConfigValues'),
    extractFunction('function moduleParamValues'),
  ].join('\n\n');
  vm.runInNewContext(script, context, { filename: APP_JS });
  return context;
}

function loadSettingsInputCoercionFunctions() {
  const elements = {
    cfgBorderWidth: { value: '0' },
    cfgKMax: { value: '99' },
    cfgStage2BoundaryMutationPercentile: { value: '180' },
  };
  const context = {
    Number,
    parseFloat,
    parseInt,
    Math,
    $(selector) {
      if (!selector.startsWith('#')) return null;
      return elements[selector.slice(1)] || null;
    },
    elements,
  };
  const script = [
    extractFunction('function coerceNumberValue'),
    extractFunction('function readBoundedNumberInput'),
    extractFunction('function readOptionalNumberInput'),
  ].join('\n\n');
  vm.runInNewContext(script, context, { filename: APP_JS });
  return context;
}

test('legacy profile aliases do not backfill canonical pitch fields', () => {
  const context = loadCanonicalProfileFunctions();
  const legacyOnlyProfile = {
    pixel_size_mm: 0.6,
    color_pixel_mm: 10.0,
  };

  context._applySettingsProfileToConfig(legacyOnlyProfile);

  assert.equal(context.config.image_sample_pitch_mm, 0.25);
  assert.equal(context.config.solver_fine_pitch_mm, 0.25);
  assert.equal(context.config.color_region_target_mm, 1.25);
});

test('settings profile load overwrites owned keys instead of merging stale live values', () => {
  const context = loadCanonicalProfileFunctions();
  context.config.color_region_target_mm = 9.5;

  context._applySettingsProfileToConfig({
    image_sample_pitch_mm: 0.4,
    solver_fine_pitch_mm: 0.4,
  });

  assert.equal(context.config.image_sample_pitch_mm, 0.4);
  assert.equal(context.config.solver_fine_pitch_mm, 0.4);
  assert.equal(
    context.config.color_region_target_mm,
    1.25,
    'missing profile-owned keys should reset to defaults instead of keeping stale live values',
  );
});

test('temporary profile reconstruction prefers durable snapshots and preserves module truth', () => {
  const context = loadTemporaryProfileFunctions();
  const profile = context.buildTemporarySettingsProfileFromRun({
    card_id: 'run-42',
    label: 'Portrait',
    config: { layer_height: 0.12, gamut_mode: 'hull', preprocessing_params: { a1_bilateral_denoise: { radius_px: 2 } } },
    result: {
      solve_start_diagnostics: {
        resolved_settings: { layer_height: 0.10 },
        active_modules: { preprocessing: ['a1_bilateral_denoise'] },
        module_settings: { a1_bilateral_denoise: { radius_px: 5 } },
      },
    },
    run_metadata: {
      recipe_snapshot: {
        profile_snapshot: {
          name: 'Captured Profile',
          settings: { layer_height: 0.08, gamut_mode: 'hue_preserving' },
          modules: { a1_bilateral_denoise: false, b1_printscale_bilateral: true },
        },
      },
    },
  }, { kind: 'solve-card', run_id: 'run-42', label: 'Portrait' });

  assert.equal(profile.kind, 'temporary');
  assert.equal(profile.name, 'Portrait');
  assert.equal(profile.settings.layer_height, 0.08);
  assert.equal(profile.settings.gamut_mode, 'hue_preserving');
  assert.equal(profile.settings.preprocessing_params.a1_bilateral_denoise.radius_px, 5);
  assert.equal(profile.modules.a1_bilateral_denoise, false);
  assert.equal(profile.modules.b1_printscale_bilateral, true);
  assert.equal(profile.source.kind, 'solve-card');
});

test('temporary profile reconstruction accepts a live solve card payload', () => {
  const context = loadTemporaryProfileFunctions();
  const profile = context.buildTemporarySettingsProfileFromRun({
    card_id: 'run-live',
    label: 'Live solve',
    config: { layer_height: 0.2, gamut_mode: 'hull' },
    profile_ref: { id: 'profile-1', name: 'Studio' },
    recipe_snapshot: {
      profile_snapshot: {
        name: 'Studio',
        settings: { layer_height: 0.09, gamut_mode: 'hue_preserving' },
        modules: { a1_bilateral_denoise: true, b1_printscale_bilateral: false },
      },
    },
    results: {
      solve_start_diagnostics: {
        resolved_settings: { layer_height: 0.11 },
        active_modules: { preprocessing: ['a1_bilateral_denoise'] },
      },
    },
  }, { kind: 'solve-card', run_id: 'run-live', label: 'Live solve' });

  assert.equal(profile.settings.layer_height, 0.09);
  assert.equal(profile.settings.gamut_mode, 'hue_preserving');
  assert.equal(profile.modules.a1_bilateral_denoise, true);
  assert.equal(profile.modules.b1_printscale_bilateral, false);
  assert.equal(profile.profile_ref.id, 'profile-1');
});

test('settings profile load snapshots after render normalization', async () => {
  const context = loadProfileLoadFlowFunctions();
  context.config.layer_height = 0.2;

  await context._doLoadSettingsProfile({
    id: 'profile-sparse-luminance',
    kind: 'named',
    name: 'Sparse luminance',
    settings: {
      layer_height: 0.1,
      luminance_mode: 'luminance_detail',
    },
    modules: {},
  }, { syncServer: false });

  assert.equal(context.config.layer_height, 0.1);
  assert.equal(context.config.luminance_detail_authoring_printability, 'absolute_finalgate');
  assert.equal(context.isSettingsProfileModified(), false);
});

test('module normalization falls back to module defaults without compat settings', () => {
  const context = loadCanonicalProfileFunctions();

  const normalized = context._normalizeSettingsProfileModules(null, {});

  assert.deepEqual(JSON.parse(JSON.stringify(normalized)), {
    a1_bilateral_denoise: false,
  });
});

const RETIRED_CAP_SHAPING_KEYS = [
  'guided_surface_mode',
  'guided_surface_radius_mm',
  'guided_surface_eps',
  'guided_surface_gaussian_sigma_mm',
  'hybrid_relax_strength',
  'hybrid_relax_radius_mm',
  'hybrid_edge_guard',
  'hybrid_underfill_bias',
  'tv_weight',
  'cap_convergence_mm',
  'cap_significant_layers',
];

function retiredProtectionKeys() {
  const retiredSubject = 'protect' + '_subject';
  const retiredMask = 'protect' + '_mask';
  return [
    `${retiredSubject}_enabled`,
    `${retiredSubject}_strength`,
    'protect' + '_confidence_floor',
    `${retiredMask}_provider`,
    `${retiredMask}_override`,
  ];
}

test('profile key whitelist excludes retired guided-surface / tv-weight settings', () => {
  const keys = loadAppProfileKeys();

  // Live keys remain.
  assert.ok(keys.includes('source_resample_kernel'));
  assert.ok(keys.includes('preprocessing_params'));
  for (const key of retiredProtectionKeys()) {
    assert.ok(!keys.includes(key), key);
  }

  // Task 2.1a: retired cap-shaping fields must not be profile keys.
  // (Top-level tv_weight only; the nested B3 preprocessing tv_weight lives
  // under preprocessing_params and is unaffected.)
  for (const key of RETIRED_CAP_SHAPING_KEYS) {
    assert.ok(!keys.includes(key), key);
  }
});

test('initial frontend config excludes retired guided-surface / tv-weight defaults', () => {
  const keys = loadInitialConfigKeys();

  // Live keys remain seeded.
  assert.ok(keys.includes('source_resample_kernel'));
  assert.ok(keys.includes('preprocessing_params'));
  for (const key of retiredProtectionKeys()) {
    assert.ok(!keys.includes(key), key);
  }

  // Task 2.1a: retired cap-shaping fields must not be seeded into config.
  for (const key of RETIRED_CAP_SHAPING_KEYS) {
    assert.ok(!keys.includes(key), key);
  }
});

test('default smoothing radius is 1 mm at default solve pitch', () => {
  const configMatch = SOURCE.match(/let config = \{[\s\S]*?\n\};/);
  assert.ok(configMatch, 'initial config block should be present');
  assert.ok(/smooth_kernel:\s*5\.0\b/.test(configMatch[0]),
    'default smoothing kernel should be 5 solve cells, i.e. 1 mm at 0.2 mm pitch');

  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');
  assert.ok(/id="cfgSmoothKernel"[^>]*value="1"/.test(html),
    'settings UI should display a 1 mm smoothing radius by default');
});


test('module posture has no special defaults after retired module removal', () => {
  const posture = loadModulePosture();

  assert.deepEqual(JSON.parse(JSON.stringify(posture)), {});
});

test('settings profiles apply preprocessing params and detect nested modifications', () => {
  const context = loadNestedProfileFunctions();
  const profileSettings = {
    preprocessing_params: {
      b1_printscale_bilateral: {
        feature_scale_multiplier: 0.8,
        sigma_range: 0.035,
        passes: 1,
      },
    },
  };

  context._applySettingsProfileToConfig(profileSettings);
  assert.deepEqual(
    JSON.parse(JSON.stringify(context.config.preprocessing_params)),
    profileSettings.preprocessing_params,
  );

  context.loadedProfileSnapshot = {
    settings: JSON.parse(JSON.stringify(context._currentSettingsSnapshot())),
    modules: {},
  };
  assert.equal(context.isSettingsProfileModified(), false);

  context.config.preprocessing_params.b1_printscale_bilateral.sigma_range = 0.04;
  assert.equal(context.isSettingsProfileModified(), true);
});

test('preprocessing module params render from nested preprocessing settings', () => {
  const context = loadModuleParamFunctions();
  const mod = context.moduleData[0];
  const values = context.moduleParamValues(mod);

  assert.equal(values.feature_scale_multiplier, 1.0);
  assert.equal(values.sigma_range, 0.035);
  assert.equal(values.passes, 1);

  context.setModuleParamValue(mod.name, mod.params.passes, 2);
  assert.equal(context.config.preprocessing_params.b1_printscale_bilateral.passes, 2);
  assert.equal(context.config.passes, undefined);
});

test('numeric module params are clamped to descriptor bounds', () => {
  const context = loadModuleParamFunctions();
  const param = {
    type: 'int',
    min: 1,
    max: 3,
  };

  assert.deepEqual(JSON.parse(JSON.stringify(context.coerceNumericParamValue(param, '99', 2))), {
    ok: true,
    value: 3,
  });
  assert.deepEqual(JSON.parse(JSON.stringify(context.coerceNumericParamValue(param, 'nope', 2))), {
    ok: false,
    value: 2,
  });
});

test('hardcoded settings inputs clamp values without rejecting valid zeroes', () => {
  const context = loadSettingsInputCoercionFunctions();

  assert.equal(
    context.readBoundedNumberInput('cfgBorderWidth', 3, { min: 0 }),
    0,
  );
  assert.equal(
    context.readBoundedNumberInput('cfgKMax', 3, {
      parse: (value) => parseInt(value, 10),
      min: 1,
      max: 7,
      integer: true,
    }),
    7,
  );
  assert.equal(
    context.readOptionalNumberInput('cfgStage2BoundaryMutationPercentile', { min: 0, max: 100 }),
    100,
  );
});

test('mandatory printability is seeded but not profile-owned', () => {
  const profileKeys = loadAppProfileKeys();
  assert.ok(!profileKeys.includes('enforce_printability'));
  assert.ok(!profileKeys.includes('cap_continuity_cleanup'));

  const configKeys = loadInitialConfigKeys();
  assert.ok(
    configKeys.includes('enforce_printability'),
    'enforce_printability missing from initial frontend config',
  );
  assert.ok(
    configKeys.includes('cap_continuity_cleanup'),
    'cap_continuity_cleanup missing from initial frontend config',
  );
});

test('luminance mode selector is profile-owned and expands to backend flags', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');
  const profileKeys = loadAppProfileKeys();
  const configKeys = loadInitialConfigKeys();

  assert.ok(profileKeys.includes('luminance_mode'), 'luminance_mode missing from SETTINGS_PROFILE_KEYS');
  assert.ok(
    profileKeys.includes('luminance_base_shading_limit_fraction'),
    'base shading limit missing from SETTINGS_PROFILE_KEYS',
  );
  assert.ok(
    profileKeys.includes('luminance_detail_authoring_printability'),
    'detail authoring printability missing from SETTINGS_PROFILE_KEYS',
  );
  assert.ok(configKeys.includes('luminance_mode'), 'luminance_mode missing from initial config');
  assert.ok(
    configKeys.includes('luminance_base_shading_limit_fraction'),
    'base shading limit missing from initial config',
  );
  assert.ok(
    configKeys.includes('luminance_detail_authoring_printability'),
    'detail authoring printability missing from initial config',
  );
  assert.ok(/id="cfgLuminanceMode"/.test(html), 'luminance mode selector missing from settings UI');
  assert.ok(/id="cfgBaseShadingLimit"/.test(html), 'base shading limit input missing from settings UI');
  assert.ok(/id="cfgBaseShadingLimitSlider"/.test(html), 'base shading limit slider missing from settings UI');
  assert.ok(/id="cfgBaseShadingLimitSuggest"/.test(html), 'base shading limit suggest button missing from settings UI');
  assert.ok(/class="[^"]*\bluminance-mode-field\b/.test(html), 'base shading limit row should be luminance-mode gated');
  assert.ok(SOURCE.includes('function updateLuminanceModeFields'), 'missing luminance mode visibility updater');

  const context = loadLuminanceModeFunctions();
  assert.equal(context.formatLuminanceMode(), 'color');

  context.config.luminance_handler_optical_authority_fraction = 1.8;
  context.applyLuminanceMode('luminance_detail');
  assert.equal(context.config.luminance_mode, 'luminance_detail');
  assert.equal(context.config.luminance_handler_enabled, true);
  assert.equal(context.config.luminance_handler_mode, 'boundary_ceiling');
  assert.equal(context.config.luminance_handler_optical_authority_fraction, 1.0);
  assert.equal(context.config.luminance_base_shading_limit_fraction, 1.0);
  assert.equal(context.config.enforce_printability, true);
  assert.equal(
    context.config.luminance_detail_authoring_printability,
    'absolute_finalgate',
  );
  assert.equal(context.formatLuminanceMode(), 'luminance');
  assert.equal(
    context.formatLuminanceBaseShadingLimitPercent(
      context.config.luminance_base_shading_limit_fraction,
    ),
    '100',
  );

  context.applyLuminanceMode('standard', { resetStandard: true });
  assert.equal(context.config.luminance_mode, 'standard');
  assert.equal(context.config.luminance_handler_enabled, false);
  assert.equal(context.config.luminance_detail_authoring_printability, 'off');
});

test('gamut mode select exposes only live options and keeps historical chroma label', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');
  const context = loadGamutModeProfileFunctions();
  const select = html.match(/<select id="cfgGamutMode"[\s\S]*?<\/select>/)?.[0] || '';
  const options = [...select.matchAll(/<option value="([^"]+)">([^<]+)<\/option>/g)].map((match) => [
    match[1],
    match[2],
  ]);

  assert.deepEqual(options, [
    ['hull', 'Nearest reachable color'],
    ['hue_preserving', 'Preserve hue'],
  ]);
  assert.doesNotMatch(select, /value="chroma"/);
  assert.match(html, /<option value="hue_preserving">Preserve hue<\/option>/);
  context._applySettingsProfileToConfig({ gamut_mode: 'hue_preserving' });

  assert.equal(context.config.gamut_mode, 'hue_preserving');
  assert.equal(context.formatGamutMode(), 'preserve hue');
  assert.equal(context.formatGamutMode('chroma'), 'reduce saturation');
});

test('legacy chroma gamut profile loads clean and would save as hue-preserving', async () => {
  const context = loadProfileLoadFlowFunctions();

  await context._doLoadSettingsProfile({
    id: 'legacy-chroma-profile',
    kind: 'named',
    name: 'Legacy chroma',
    settings: {
      gamut_mode: 'chroma',
    },
    modules: {},
  }, { syncServer: false });

  assert.equal(context.config.gamut_mode, 'hue_preserving');
  assert.equal(context.isSettingsProfileModified(), false);
  assert.equal(context._currentSettingsSnapshot().gamut_mode, 'hue_preserving');
});

test('chroma weight slider maps detents to raw multipliers', () => {
  const context = loadChromaWeightControlFunctions();

  for (let quarterSteps = -12; quarterSteps <= 12; quarterSteps += 1) {
    const position = quarterSteps / 4;
    const weight = context.chromaWeightFromSliderPosition(position);
    assert.ok(
      Math.abs(context.chromaWeightToSliderPosition(weight) - position) < 1e-12,
      `detent ${position} should round-trip through raw weight`,
    );
  }

  assert.equal(context.chromaWeightFromSliderPosition(0), 1.0);
});

test('legacy chroma weight load preserves raw value without dirtying profile', () => {
  const context = loadChromaWeightControlFunctions();
  context.config.chroma_weight = 5.0;
  context.loadedProfileSnapshot = {
    settings: { chroma_weight: 5.0 },
    modules: {},
  };

  context.syncChromaWeightControlFromConfig();

  assert.equal(
    Number(context.elements.cfgChromaWeight.value),
    Math.log2(5.0),
  );
  assert.equal(context.elements.cfgChromaWeightReadout.textContent, '5.00');
  assert.equal(context._currentSettingsSnapshot().chroma_weight, 5.0);
  assert.equal(context.config.chroma_weight, 5.0);
  assert.equal(context.isSettingsProfileModified(), false);
});

test('settings profile reload drains stale config sync before applying saved values', async () => {
  const context = loadProfileLoadFlowFunctions();
  context.apiConnected = true;
  context.config.t_max = 4.0;

  let releaseStaleSync;
  context._configSyncChain = new Promise((resolve) => {
    releaseStaleSync = () => {
      context.config.t_max = 4.0;
      resolve();
    };
  });
  let syncedValue = null;
  context.syncConfigToServer = async () => {
    syncedValue = context.config.t_max;
  };

  const loading = context._doLoadSettingsProfile({
    id: 'profile-reload-race',
    kind: 'named',
    name: 'Reload race',
    settings: { t_max: 3.0 },
    modules: {},
  });
  await Promise.resolve();
  releaseStaleSync();
  await loading;

  assert.equal(context.config.t_max, 3.0);
  assert.equal(syncedValue, 3.0);
  assert.equal(context.isSettingsProfileModified(), false);
});

test('settings profile reload does not reread stale controls during module refresh', async () => {
  const context = loadProfileLoadFlowFunctions();
  context.moduleData = [{ name: 'demo_module', default_enabled: false }];
  context.domTMax = 4.0;
  context.config.t_max = 4.0;
  context.refreshModuleDrivenViews = () => {
    context.config.t_max = context.domTMax;
  };

  await context._doLoadSettingsProfile({
    id: 'profile-stale-dom',
    kind: 'named',
    name: 'Stale DOM',
    settings: { t_max: 3.0 },
    modules: {},
  }, { syncServer: false });

  assert.equal(context.config.t_max, 3.0);
  assert.equal(context.domTMax, 3.0);
  assert.equal(context.isSettingsProfileModified(), false);
});

test('settings profile load does not report clean success when server sync fails', async () => {
  const context = loadProfileLoadFlowFunctions();
  context.apiConnected = true;
  let syncOptions = null;
  context.syncConfigToServer = async (options) => {
    syncOptions = options;
    throw new Error('config write failed');
  };

  await assert.rejects(
    context._doLoadSettingsProfile({
      id: 'profile-failed-sync',
      kind: 'named',
      name: 'Failed sync',
      settings: { t_max: 3.0 },
      modules: {},
    }),
    /config write failed/,
  );

  assert.equal(syncOptions.throwOnError, true);
  assert.equal(context.loadedProfileRef, null);
  assert.equal(context.loadedProfileSnapshot, null);
});

test('settings profile load restores local state when module persistence fails', async () => {
  const context = loadProfileLoadFlowFunctions();
  context.apiConnected = true;
  context.moduleData = [{ name: 'demo_module', default_enabled: false }];
  context.moduleState = { demo_module: false };
  context.config.t_max = 4.0;
  let moduleWrites = 0;
  context.setModuleState = async (state) => {
    moduleWrites += 1;
    if (moduleWrites === 1) throw new Error('module write failed');
    return { state };
  };

  await assert.rejects(
    context._doLoadSettingsProfile({
      id: 'profile-module-failed',
      kind: 'temporary',
      name: 'Module failure',
      settings: { t_max: 3.0 },
      modules: { demo_module: true },
    }),
    /module write failed/,
  );

  assert.equal(context.config.t_max, 4.0);
  assert.equal(context.moduleState.demo_module, false);
  assert.equal(moduleWrites, 2, 'rollback should restore the prior persisted module state');
  assert.equal(context.loadedProfileRef, null);
  assert.equal(context.temporarySettingsProfile, null);
});

test('off-detent chroma weight load preserves raw value through read-back', () => {
  const context = loadChromaWeightControlFunctions();
  context.config.chroma_weight = 1.5;
  context.loadedProfileSnapshot = {
    settings: { chroma_weight: 1.5 },
    modules: {},
  };

  context.syncChromaWeightControlFromConfig();

  assert.equal(context.elements.cfgChromaWeightReadout.textContent, '1.50');
  assert.equal(
    Number(context.elements.cfgChromaWeight.value),
    Math.log2(1.5),
  );
  assert.equal(context._currentSettingsSnapshot().chroma_weight, 1.5);
  assert.equal(context.isSettingsProfileModified(), false);
});

test('chroma weight user input stores the raw multiplier', () => {
  const context = loadChromaWeightControlFunctions();

  assert.equal(context.applyChromaWeightSliderInput(1), 2.0);
  assert.equal(context.config.chroma_weight, 2.0);
  assert.equal(context.elements.cfgChromaWeight.value, '1');
  assert.equal(context.elements.cfgChromaWeightReadout.textContent, '2.00');

  assert.equal(context.applyChromaWeightSliderInput(3), 8.0);
  assert.equal(context.config.chroma_weight, 8.0);
  assert.equal(context.elements.cfgChromaWeight.value, '3');
  assert.equal(context.elements.cfgChromaWeightReadout.textContent, '8.00');
});

test('chroma weight settings row uses centered log slider with raw readout', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');
  const row = html.match(/<tr class="advanced-setting"><td title="Trade-off between color accuracy[\s\S]*?id="cfgChromaWeightReadout"[\s\S]*?<\/tr>/)?.[0] || '';

  assert.ok(row.includes('Chroma weight <span class="stg-range">0.125–8.0</span>'));
  assert.ok(row.includes('class="chroma-weight-control"'));
  assert.ok(row.includes('class="chroma-weight-labels"'));
  assert.ok(row.includes('<span>Tone</span>'));
  assert.ok(row.includes('<span>Color</span>'));
  assert.ok(row.includes('type="range"'));
  assert.ok(row.includes('class="control-slider slider-with-center"'));
  assert.ok(row.includes('min="-3"'));
  assert.ok(row.includes('max="3"'));
  assert.ok(row.includes('step="0.25"'));
  assert.ok(row.includes('data-center="0"'));
  assert.ok(row.includes('id="cfgChromaWeightReadout"'));
  assert.ok(!row.includes('type="text"'));
  assert.ok(!row.includes('class="range-row"'));
});

test('appearance model selector uses color model product names', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');

  assert.ok(
    /<option value="historical_spline">Color Model v1<\/option>/.test(html),
    'historical spline backend should be labeled Color Model v1',
  );
  assert.ok(
    /<option value="photo_stack_bundle" selected>Color Model v2<\/option>/.test(html),
    'photo stack backend should be labeled Color Model v2',
  );
  assert.equal(/Historical spline|Latest photo stack fit/.test(html), false);
});

test('detail layer limit is profile-owned and seeded in initial config', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');
  const profileKeys = loadAppProfileKeys();
  assert.ok(
    profileKeys.includes('detail_cap_max_layers'),
    'detail_cap_max_layers missing from SETTINGS_PROFILE_KEYS',
  );
  assert.ok(
    !profileKeys.includes('detail_cap_enabled'),
    'detail_cap_enabled should not be profile-owned now that detail cap is mandatory',
  );
  assert.ok(
    !profileKeys.includes('detail_cap_coverage'),
    'retired detail_cap_coverage should not be profile-owned',
  );
  assert.ok(
    profileKeys.includes('detail_cap_pitch_mm'),
    'detail_cap_pitch_mm missing from SETTINGS_PROFILE_KEYS',
  );
  for (const key of [
    'detail_cap_smoothing_enabled',
    'detail_cap_smoothing_exact_speckle_max_px',
    'detail_cap_smoothing_cumulative_component_max_px',
    'detail_cap_smoothing_cumulative_hole_max_px',
  ]) {
    assert.ok(profileKeys.includes(key), `${key} missing from SETTINGS_PROFILE_KEYS`);
  }
  assert.ok(
    !profileKeys.includes('stage4_independent_detail_surface'),
    'retired independent detail mode should not be profile-owned',
  );
  assert.ok(
    !profileKeys.includes('stage4_optical_detail_surface'),
    'retired optical detail mode should not be profile-owned',
  );

  const configKeys = loadInitialConfigKeys();
  assert.ok(
    configKeys.includes('detail_cap_max_layers'),
    'detail_cap_max_layers missing from initial frontend config',
  );
  assert.ok(
    /detail_cap_max_layers:\s*5/.test(SOURCE),
    'initial frontend config should default detail depth to 5 layers',
  );
  assert.ok(
    /id="cfgDetailCapMaxLayers"[^>]*value="5"/.test(html),
    'detail depth settings input should default to 5 layers',
  );
  assert.ok(
    SOURCE.includes('_set("#cfgDetailCapMaxLayers", config.detail_cap_max_layers ?? 5)'),
    'settings render fallback should use the 5-layer detail depth default',
  );
  assert.ok(
    configKeys.includes('detail_cap_enabled'),
    'initial frontend config should keep the mandatory detail-cap invariant',
  );
  assert.ok(
    !configKeys.includes('detail_cap_coverage'),
    'retired detail_cap_coverage should not be in initial frontend config',
  );
  assert.ok(
    configKeys.includes('detail_cap_pitch_mm'),
    'detail_cap_pitch_mm missing from initial frontend config',
  );
  for (const key of [
    'detail_cap_smoothing_enabled',
    'detail_cap_smoothing_exact_speckle_max_px',
    'detail_cap_smoothing_cumulative_component_max_px',
    'detail_cap_smoothing_cumulative_hole_max_px',
  ]) {
    assert.ok(configKeys.includes(key), `${key} missing from initial frontend config`);
  }
  assert.ok(
    !configKeys.includes('stage4_independent_detail_surface'),
    'retired independent detail mode should not be in initial frontend config',
  );
  assert.ok(
    !configKeys.includes('stage4_optical_detail_surface'),
    'retired optical detail mode should not be in initial frontend config',
  );
  assert.ok(!/id="cfgDetailCapCoverage"/.test(html), 'retired detail coverage slider should be absent');
  assert.ok(!/id="cfgDetailCapEnabled"/.test(html), 'detail cap enable checkbox should be absent');
  assert.ok(!/detail-cap-toggle-field/.test(html), 'detail cap toggle row should be absent');
  assert.ok(!SOURCE.includes('cfgDetailCapEnabled'), 'app should not read or render the removed detail cap checkbox');
  const detailLayerInput = html.match(/<input[^>]*id="cfgDetailCapMaxLayers"[^>]*>/)?.[0] || '';
  assert.ok(detailLayerInput.includes('type="number"'), 'detail layer limit should be numeric');
  assert.ok(detailLayerInput.includes('min="0"'), 'detail layer limit should be non-negative');
  assert.ok(detailLayerInput.includes('step="1"'), 'detail layer limit should be integer-stepped');
  assert.ok(
    SOURCE.includes('/^[0-9]+$/.test(detailLayerRaw)'),
    'settings reader should reject fractional detail layer counts instead of flooring them',
  );
});

test('boundary mutation pass limit is profile-owned and client-clamped', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');
  const profileKeys = loadAppProfileKeys();
  assert.ok(
    profileKeys.includes('stage2_boundary_mutation_max_passes'),
    'stage2_boundary_mutation_max_passes missing from SETTINGS_PROFILE_KEYS',
  );
  assert.ok(
    profileKeys.includes('stage2_boundary_mutation_enabled'),
    'stage2_boundary_mutation_enabled missing from SETTINGS_PROFILE_KEYS',
  );
  assert.ok(
    !profileKeys.includes('stage2_boundary_mutation_segment_mode'),
    'retired stage2_boundary_mutation_segment_mode should not be profile-owned',
  );
  assert.ok(
    !profileKeys.includes('stage2_boundary_mutation_edge_run_mode'),
    'retired stage2_boundary_mutation_edge_run_mode should not be profile-owned',
  );

  const configKeys = loadInitialConfigKeys();
  assert.ok(
    configKeys.includes('stage2_boundary_mutation_max_passes'),
    'stage2_boundary_mutation_max_passes missing from initial frontend config',
  );
  assert.ok(
    /stage2_boundary_mutation_enabled:\s*true/.test(SOURCE),
    'initial frontend config should default boundary mutation on',
  );
  assert.ok(
    /id="cfgStage2BoundaryMutation" checked/.test(html),
    'boundary mutation checkbox should default checked',
  );
  assert.ok(
    /stage2_boundary_mutation_max_passes:\s*1/.test(SOURCE),
    'initial frontend config should default mutation passes to 1',
  );
  assert.ok(
    /id="cfgStage2BoundaryMutationMaxPasses"[^>]*placeholder="1"/.test(html),
    'mutation passes input should show the default placeholder',
  );
  assert.ok(
    /Mutation passes <span class="stg-range">1–16<\/span>/.test(html),
    'mutation passes row should expose the 1-16 range hint',
  );
  assert.ok(
    SOURCE.includes('setOptionalNumberInput("cfgStage2BoundaryMutationMaxPasses", config.stage2_boundary_mutation_max_passes ?? 1)'),
    'settings render fallback should use the 1-pass default',
  );
  assert.ok(
    SOURCE.includes('readOptionalNumberInput("cfgStage2BoundaryMutationMaxPasses", { min: 1, max: 16 }) ?? 1'),
    'settings readback should client-clamp mutation passes to 1-16',
  );
  assert.ok(
    /Mutation current-dE percentile[\s\S]*placeholder="off"/.test(html),
    'current-dE percentile should default to off',
  );
  assert.ok(
    /Mutation min gain[\s\S]*placeholder="0\.010"/.test(html),
    'min gain should show the 0.010 default placeholder',
  );
  assert.ok(
    /Mutation min contact[\s\S]*placeholder="off"/.test(html),
    'min contact should expose the contact semantics',
  );
});

test('boundary cap appearance budget is profile-owned and exposed only for prototype mode', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');
  const profileKeys = loadAppProfileKeys();
  assert.ok(
    profileKeys.includes('boundary_cap_de_budget'),
    'boundary_cap_de_budget missing from SETTINGS_PROFILE_KEYS',
  );

  const configKeys = loadInitialConfigKeys();
  assert.ok(
    configKeys.includes('boundary_cap_de_budget'),
    'boundary_cap_de_budget missing from initial frontend config',
  );
  assert.ok(
    /id="cfgBoundaryCapDeBudget"/.test(html),
    'boundary cap appearance budget input should exist',
  );
  assert.ok(
    /cap-appearance-bound-field/.test(html),
    'budget field should be scoped to appearance-bounded cap mode',
  );
  assert.ok(
    SOURCE.includes('isAppearanceBounded = mode === "appearance_bounded_smooth"'),
    'budget field visibility should be tied to prototype cap mode',
  );
});

test('fixed thickness cap mode is absent from frontend config and profiles', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');
  const profileKeys = loadAppProfileKeys();
  const configKeys = loadInitialConfigKeys();

  assert.ok(!profileKeys.includes('cap_fixed_thickness_mm'));
  assert.ok(!configKeys.includes('cap_fixed_thickness_mm'));
  assert.ok(!html.includes('cfgCapFixedThickness'));
  assert.ok(!html.includes('value="fixed"'));
  assert.ok(!SOURCE.includes('cfgCapFixedThickness'));
  assert.ok(!SOURCE.includes('cap_fixed_thickness_mm'));
});

test('color region target is exposed as a settings control', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');

  assert.ok(
    /id="cfgColorRegionTarget"/.test(html),
    'color_region_target_mm should have a user-facing settings input',
  );
  assert.ok(
    SOURCE.includes('_set("#cfgColorRegionTarget", config.color_region_target_mm ?? 0.60)'),
    'settings render should populate color region target',
  );
  assert.ok(
    SOURCE.includes('config.color_region_target_mm = readBoundedNumberInput("cfgColorRegionTarget"'),
    'settings reader should write color region target back to config',
  );
  assert.ok(
    SOURCE.includes('{ label: "Color region target", value: `${settings.color_region_target_mm ?? 0.60} mm` }'),
    'run-essentials block should show color region target from the run snapshot',
  );
});

test('retired boundary mutation grouping controls are removed from settings UI', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');
  assert.ok(
    !/cfgStage2BoundaryMutationEdgeRunMode/.test(html),
    'cfgStage2BoundaryMutationEdgeRunMode should no longer be in index.html',
  );
  assert.ok(
    !/cfgStage2BoundaryMutationEdgeRunMode/.test(SOURCE),
    'cfgStage2BoundaryMutationEdgeRunMode should no longer be referenced in app.js',
  );
  assert.ok(
    !/cfgStage2BoundaryMutationSegmentMode/.test(html),
    'cfgStage2BoundaryMutationSegmentMode should no longer be in index.html',
  );
  assert.ok(
    !/cfgStage2BoundaryMutationSegmentMode/.test(SOURCE),
    'cfgStage2BoundaryMutationSegmentMode should no longer be referenced in app.js',
  );
  assert.ok(
    !/Mutation grouping/.test(html),
    'dead Mutation grouping row should be removed',
  );
});

test('layer-step smoothing is removed from settings and profile paths', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');
  const profileKeys = loadAppProfileKeys();

  assert.ok(!html.includes('cfgCapLayerSpace'), 'cfgCapLayerSpace should not be in index.html');
  assert.ok(!html.includes('Layer-step smoothing'), 'Layer-step smoothing should not be user-facing');
  assert.ok(!SOURCE.includes('cfgCapLayerSpace'), 'cfgCapLayerSpace should not be referenced in app.js');
  assert.ok(!SOURCE.includes('cap_layer_space'), 'cap_layer_space should not be referenced in app.js');
  assert.ok(
    !profileKeys.includes('cap_layer_space'),
    'cap_layer_space should not be persisted as a Settings Profile control',
  );
});

test('palette suggestions expose only the thorough search path', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');

  for (const removed of [
    'suggestSearchMode',
    'creationQualitySettings',
    'paletteQualityWeightDe',
    'paletteQualityWeightFrag',
    'paletteQualityWeightGroup',
    'paletteQualityWeightSwap',
    'cfgQualityWeightDe',
    'cfgQualityWeightFrag',
    'cfgQualityWeightGroup',
    'cfgQualityWeightSwap',
    'Quality Weights',
  ]) {
    assert.ok(!html.includes(removed), `${removed} should not be user-facing`);
    assert.ok(!SOURCE.includes(removed), `${removed} should not be referenced in app.js`);
  }

  assert.ok(
    !SOURCE.includes('search_mode: "thorough"'),
    'palette suggestion requests should not send the removed search mode field',
  );
  assert.ok(
    !SOURCE.includes('payload.quality_weights'),
    'palette suggestion requests should not send user-tunable quality weights',
  );
  assert.ok(
    SOURCE.includes('suggestion_mean_de'),
    'palette suggestion cards should use the honest suggestion metric field',
  );
  assert.ok(
    SOURCE.includes('max_swaps: swapCount'),
    'palette suggestion requests should always send max_swaps, including zero',
  );
  assert.ok(
    SOURCE.includes('suggestions.alternatives || []'),
    'tiered palette results should stage top-level alternatives before ladder entries',
  );
  assert.ok(
    SOURCE.includes('seenSets.has(key)'),
    'tiered palette staging should dedupe alternatives and ladder entries by filament set',
  );
  assert.ok(
    SOURCE.includes('Max colors capped to ${perLoadCapped.capacity} by AMS capacity'),
    'tiered palette results should warn when Max colors is physically capped',
  );
  assert.ok(
    html.includes('suggestCapacityNote') && SOURCE.includes('suggestCapacityNote'),
    'tiered palette clamp note should be rendered in the Suggested Palettes panel',
  );
  assert.ok(
    !html.includes('palettePreviewResolution') && !SOURCE.includes('palettePreviewResolution'),
    'retired Preview Resolution control should be removed from palette suggestion settings',
  );
  assert.ok(
    html.includes('Min added-color gain'),
    'palette suggestion threshold should use the added-color label',
  );
});

test('palette creation guidance is mode-specific below the mode toggle', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');

  assert.ok(
    html.includes('id="creationModeNote"'),
    'palette guidance should live below the Auto-Suggest / Manual mode toggle',
  );
  for (const removedText of [
    'White Base/Cap Filament:',
    'Build a palette of color filaments below.',
    'id="creationInfo"',
  ]) {
    assert.ok(!html.includes(removedText), `${removedText} should not appear in the palette tab shell`);
    assert.ok(!SOURCE.includes(removedText), `${removedText} should not be rendered by app.js`);
  }
  assert.ok(
    SOURCE.includes('Set max colored filaments per load, extra color-load tiers, and suggestions to use.'),
    'auto-suggest guidance should explain palette suggestion controls',
  );
  assert.ok(
    html.includes('Extra color loads') && !html.includes('AMS swaps'),
    'auto-suggest controls should avoid swap wording for search load tiers',
  );
  assert.ok(
    html.includes('Compute all load tiers') && !html.includes('Force Compute All Swaps'),
    'palette suggestion settings should use load-tier wording instead of swap wording',
  );
  assert.ok(
    SOURCE.includes('base load') && SOURCE.includes('extra load'),
    'tiered suggestion card labels should use load wording instead of swap wording',
  );
  assert.ok(
    html.includes('matches the Solve Mode') && !html.includes('matches your the Solve Mode'),
    'auto-suggest guidance should use the corrected Solve Mode wording',
  );
  assert.ok(
    SOURCE.includes('Select filaments to be included in the manual palette.'),
    'manual guidance should explain manual filament selection',
  );
});

test('user-facing quality summaries use color rmse instead of coverage percent', () => {
  assert.ok(
    SOURCE.includes('function formatColorRmse'),
    'app should have a shared RMSE formatter',
  );
  assert.ok(
    SOURCE.includes('source_rms_de'),
    'app should read the source_rms_de metric from solve/compare responses',
  );
  for (const removedText of [
    '% cov',
    '% coverage',
    '% within',
    'within · dE',
    'coverage · dE',
  ]) {
    assert.ok(
      !SOURCE.includes(removedText),
      `${removedText} should not be used as a user-facing quality summary`,
    );
  }
});

test('debug-map and printability threshold controls are internalized', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');
  const profileKeys = loadAppProfileKeys();

  for (const id of [
    'cfgEmitPressureDiagnostics',
    'cfgEmitGeometryAttribution',
    'cfgPrintabilityMinExtrusionWidth',
    'cfgPrintabilityMinLineLength',
    'cfgPrintabilityPreferredLineLength',
  ]) {
    assert.ok(!html.includes(id), `${id} should not be user-facing`);
    assert.ok(!SOURCE.includes(`_set("#${id}"`), `${id} should not be rendered from config`);
    assert.ok(!SOURCE.includes(`$("#${id}")`), `${id} should not be read from UI`);
  }

  for (const key of [
    'emit_pressure_diagnostics',
    'emit_geometry_attribution',
    'emit_blueprint_printability',
    'printability_minimum_extrusion_width_mm',
    'printability_minimum_line_length_mm',
    'printability_preferred_line_length_mm',
  ]) {
    assert.ok(
      !profileKeys.includes(key),
      `${key} should not be persisted as a Settings Profile control`,
    );
  }

  assert.ok(
    /emit_pressure_diagnostics:\s*false/.test(SOURCE),
    'pressure diagnostics should default off in the app',
  );
  assert.ok(
    /emit_geometry_attribution:\s*false/.test(SOURCE),
    'geometry attribution should default off in the app',
  );
  assert.ok(
    /emit_blueprint_printability:\s*true/.test(SOURCE),
    'blueprint printability diagnostics should default on in the app',
  );
  assert.ok(
    SOURCE.includes('config.emit_pressure_diagnostics = false;'),
    'web config sync should keep pressure diagnostics off without a UI control',
  );
  assert.ok(
    SOURCE.includes('config.emit_geometry_attribution = false;'),
    'web config sync should keep geometry attribution off without a UI control',
  );
  assert.ok(
    SOURCE.includes('config.emit_blueprint_printability = true;'),
    'web config sync should keep blueprint printability diagnostics on',
  );
});

test('bundled white cap view exposes total boundary and detail surfaces', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');

  assert.ok(/data-view="white_cap"[^>]*>White Cap/.test(html));
  assert.ok(/id="solveWhiteCapControls"/.test(html));
  assert.ok(/data-solve-white-cap-view="cap_map"[^>]*>Total/.test(html));
  assert.ok(/data-solve-white-cap-view="boundary_cap_map"[^>]*>Boundary/.test(html));
  assert.ok(/data-solve-white-cap-view="detail_cap_map"[^>]*>Detail/.test(html));
  assert.ok(SOURCE.includes('boundary_cap_map_url'));
  assert.ok(SOURCE.includes('detail_cap_map_url'));
  assert.ok(SOURCE.includes('Total White Cap'));
  assert.ok(SOURCE.includes('Boundary Cap'));
  assert.ok(SOURCE.includes('Detail Cap'));
});

test('printer config exposes printability line lengths per nozzle', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');

  assert.ok(/Min Len/.test(html), 'printer nozzle table should show hard minimum line length');
  assert.equal(/Pref Len/.test(html), false, 'printer nozzle table should not show preferred line length');
  assert.equal(/Line W/.test(html), false, 'nominal line width is not a product-control setting');
  assert.equal(/Max W/.test(html), false, 'maximum line width is not a product-control setting');
  assert.equal(SOURCE.includes('class="nz-lw"'), false, 'nominal line width should not be user-editable');
  assert.equal(SOURCE.includes('class="nz-max-lw"'), false, 'maximum line width should not be user-editable');
  assert.ok(SOURCE.includes('class="nz-min-ll"'), 'printer config should render min line length inputs');
  assert.equal(SOURCE.includes('class="nz-pref-ll"'), false, 'printer config should not render preferred line length inputs');
  assert.ok(SOURCE.includes('min_line_length'), 'printer config should persist min_line_length');
  assert.equal(SOURCE.includes('preferred_line_length'), false, 'printer config should not persist preferred_line_length');
});

test('printer config modal preserves selected printer across rerenders', () => {
  assert.ok(
    SOURCE.includes('let printerConfigEditingId = null'),
    'printer config should track the modal-selected printer separately from persisted active state',
  );
  assert.ok(
    SOURCE.includes('selectPrinterConfigId(sel.value);'),
    'printer selector changes should update the modal-selected printer before rerendering',
  );
  assert.ok(
    SOURCE.includes('printerConfigEditingId = id;'),
    'new printer creation should select the newly-created printer before rerendering',
  );
  assert.ok(
    SOURCE.includes('printersData.active_printer_id = printerConfigEditingId;'),
    'saving the modal should persist the modal-selected printer as active',
  );
  assert.ok(
    SOURCE.includes('updatePrinterConfigDropdownLabel(printer.id, nextName);'),
    'renaming a printer should update the selected dropdown option immediately',
  );
  assert.ok(
    SOURCE.includes('printerDeleteConfirmPending = true'),
    'printer delete should require an armed confirmation click before deleting',
  );
  assert.ok(
    SOURCE.includes('pcDelete.textContent = "Confirm?"'),
    'printer delete should visibly switch into a confirmation state',
  );
  assert.ok(
    SOURCE.includes('setTimeout(resetPrinterDeleteConfirm'),
    'printer delete confirmation should expire automatically',
  );
});

test('conditional experimental settings rows are wired for visibility updates', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');

  assert.ok(
    /class="[^"]*\bboundary-mutation-field\b/.test(html),
    'boundary mutation parameter rows should be hideable as a group',
  );
  assert.ok(
    /class="[^"]*\bdetail-surface-field\b/.test(html),
    'detail-only parameter rows should be hideable as a group',
  );
  assert.ok(
    SOURCE.includes('function updateBoundaryMutationFields'),
    'missing boundary mutation visibility updater',
  );
  assert.ok(
    SOURCE.includes('function updateStage4DetailFields'),
    'missing stage 4 detail visibility updater',
  );
  assert.ok(
    /updateBoundaryMutationFields\(\);/.test(SOURCE),
    'boundary mutation visibility updater should run from settings refresh/change',
  );
  assert.ok(
    /updateStage4DetailFields\(\);/.test(SOURCE),
    'detail visibility updater should run from settings refresh/change',
  );
});

test('retired grouping module controls are absent from the staged UI', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');

  assert.ok(
    !/data-settings-group="grouping"/.test(html),
    'retired grouping settings section should be removed',
  );
  assert.ok(
    !/data-spage="grouping"/.test(html),
    'retired grouping drawer nav should be removed',
  );
  assert.ok(
    SOURCE.includes('const MODULE_UI_VISIBLE_SLOTS = new Set(["preprocessing"]);'),
    'dynamic module settings should render preprocessing only',
  );
  assert.ok(
    !SOURCE.includes('groupingSettingsContainer'),
    'module activation panel should not expose the retired slot',
  );
});

test('settings drawer does not expose an empty Output page', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');

  assert.ok(
    !/data-spage="output"/.test(html),
    'settings drawer should not show the orphaned Output navigation pill',
  );
  assert.ok(
    !/data-bucket="output"/.test(html),
    'settings grid should not contain a hidden Output settings bucket',
  );
  assert.ok(
    !SOURCE.includes('Changed Settings · Output'),
    'run setting differences should use the White Cap category instead of phantom Output',
  );
  assert.ok(
    SOURCE.includes('"white-cap": "Changed Settings · White Cap"'),
    'run setting differences should expose a White Cap category',
  );
});

test('luminance mode forces smooth cap without forgetting color-mode cap style', () => {
  assert.ok(
    SOURCE.includes('const COLOR_CAP_MODE_STORAGE_KEY = "prisma_color_cap_mode";'),
    'the last color-mode cap style should have a localStorage key',
  );
  assert.ok(
    SOURCE.includes('let lastColorCapMode = loadLastColorCapMode(config.cap_mode || "appearance_bounded_smooth");'),
    'the UI should keep a color-mode cap-style memory separate from the active solve payload',
  );
  assert.ok(
    SOURCE.includes('function saveLastColorCapMode(mode)'),
    'the color-mode cap-style memory should survive hard refreshes while luminance is active',
  );
  assert.ok(
    SOURCE.includes('let capModeForcedByLuminance = false;'),
    'the UI should distinguish luminance-forced Smooth from a user-selected Smooth cap',
  );
  assert.ok(
    SOURCE.includes('const configAlreadyLuminance = normalizeLuminanceMode(config.luminance_mode) === "luminance_detail";'),
    'reloads while already in luminance should not overwrite remembered color cap style with forced Smooth',
  );
  assert.ok(
    /if \(!capModeForcedByLuminance && !configAlreadyLuminance\) \{[\s\S]*?saveLastColorCapMode\(capMode\.value \|\| config\.cap_mode \|\| lastColorCapMode\);[\s\S]*?\}[\s\S]*?capMode\.value = "smooth_variable";[\s\S]*?capModeForcedByLuminance = true;/.test(SOURCE),
    'entering luminance from color should force the DOM control to Smooth after remembering the prior color cap style once',
  );
  assert.ok(
    /if \(capMode && capModeForcedByLuminance\) \{[\s\S]*?capMode\.value = restored;[\s\S]*?capModeForcedByLuminance = false;/.test(SOURCE),
    'returning to color should restore only values that luminance forced',
  );
  assert.ok(
    /if \(normalizeLuminanceMode\(getSolveModeControlValue\(\)\) === "luminance_detail"\) \{[\s\S]*?setSettingsSummary\("capSummary", "", ""\);/.test(SOURCE),
    'luminance mode should suppress the boundary-cap style comparison summary',
  );
});

test('app prompt dialog supports semantic titles', () => {
  assert.ok(
    /function appPrompt\(message, defaultValue = "", \{ title = "Input", validate = null \} = \{\}\)/.test(SOURCE),
    'shared prompt helper should accept an optional title',
  );
  assert.ok(
    /appPrompt\(title, nextDefault, \{ title: "Settings Profile" \}\)/.test(SOURCE),
    'settings profile name prompts should use a semantic dialog title',
  );
});

test('settings profile choice dialogs use semantic titles', () => {
  assert.ok(
    SOURCE.includes('{ title: "Save Settings Profile" }'),
    'overwrite/save-as dialog should use a semantic Settings Profile save title',
  );
  assert.ok(
    SOURCE.includes('{ title: "Unsaved Settings Profile" }'),
    'navigation guard dialog should use a semantic unsaved-profile title',
  );
});

test('temporary profile save prompt offers a named conversion instead of system-default wording', () => {
  assert.ok(
    SOURCE.includes('const isTemporary = current?.kind === "temporary";'),
    'save flow should recognize the session-only profile kind',
  );
  assert.ok(
    SOURCE.includes('This TEMP profile is session-only. Save it as a new named Settings Profile?'),
    'temporary profiles should explain that Save As converts them to a named profile',
  );
});

test('temporary profile rows cannot present a startup-default control', () => {
  assert.ok(
    SOURCE.includes('${profile.kind !== "temporary" ? `'),
    'TEMP rows should omit the startup-default star action',
  );
});

test('settings profile startup badge uses concise label', () => {
  assert.ok(
    SOURCE.includes('badges.push({ label: "startup", accent: true, warn: false });'),
    'startup-default profile badge should read "startup"',
  );
  assert.equal(
    SOURCE.includes('badges.push({ label: "startup default"'),
    false,
    'settings profile badge should not use the longer "startup default" label',
  );
});

test('preprocessing module section avoids redundant subsection label', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');

  assert.ok(
    /<h4 class="settings-section-head">Preprocessing Modules<\/h4>/.test(html),
    'preprocessing section should use the concise module-focused heading',
  );
  assert.equal(
    /<h5 class="settings-subsection-head">Pre-processing Modules<\/h5>/.test(html),
    false,
    'preprocessing section should not repeat its own module heading as a subsection',
  );
  assert.equal(
    /Source Sampling/.test(html),
    false,
    'preprocessing section should not use the stale source-sampling subsection label',
  );
});

test('preprocessing module preset is visible while detailed params are advanced', () => {
  assert.equal(
    /tr\.className = "module-preset-row";/.test(SOURCE),
    true,
    'enabled preprocessing modules should show their preset selector outside advanced settings',
  );
  assert.ok(
    SOURCE.includes('tr.classList.add("advanced-setting", "module-advanced-param-row");'),
    'detailed preprocessing module params should be hidden until advanced settings are shown',
  );
  assert.equal(
    SOURCE.includes('tr.className = "advanced-setting module-preset-row";'),
    false,
    'the preset selector itself should not be hidden behind the global advanced-settings toggle',
  );
});

test('expanded settings drawer remains an overlay instead of full viewport', () => {
  const styleCssPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/style.css',
  );
  const css = fs.readFileSync(styleCssPath, 'utf8');

  assert.ok(
    /\.settings-drawer\s*\{[\s\S]*?top:\s*14px;/.test(css),
    'settings drawer should leave a small top gap below the main bar',
  );
  assert.ok(
    css.includes('width: min(calc(100% - 32px), var(--settings-drawer-width));'),
    'responsive settings drawer should leave underlying tab visible when viewport allows',
  );
  assert.ok(
    SOURCE.includes('drawer.style.setProperty("--settings-drawer-width", "1280px");'),
    'drawer distribution should start from the maximum planned width',
  );
  assert.ok(
    SOURCE.includes('targetDrawerWidth - drawerBodyPadding - drawerChrome'),
    'drawer should plan columns from its intended width, not a transitional slide width',
  );
  assert.ok(
    SOURCE.includes('parseFloat(measureStyle.paddingTop)'),
    'expanded drawer column budgeting should subtract body padding from clientHeight',
  );
  assert.ok(
    SOURCE.includes('drawer?.style.setProperty('),
    'expanded drawer should shrink to the number of columns actually used',
  );
});

test('image library resize control does not stretch the title bar', () => {
  const styleCssPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/style.css',
  );
  const css = fs.readFileSync(styleCssPath, 'utf8');

  assert.ok(
    /\.pane-title-bar,\s*\.library-title-bar\s*\{[\s\S]*?min-height:\s*28px;/.test(css),
    'image pane title bars should share the same compact height',
  );
  assert.ok(
    /\.library-resize-btn\s*\{[\s\S]*?height:\s*20px;/.test(css),
    'image library resize button should fit inside the compact title bar',
  );
});

test('image library exposes a refresh control that rescans the server folder', () => {
  const indexHtmlPath = path.resolve(
    __dirname,
    '../../Prisma/generator/app/index.html',
  );
  const html = fs.readFileSync(indexHtmlPath, 'utf8');
  const refreshSource = extractFunction('async function refreshImageLibrary');

  assert.ok(
    html.includes('id="imageLibraryRefreshBtn"'),
    'image library title bar should include a refresh button',
  );
  assert.ok(
    SOURCE.includes('imageLibraryRefreshBtn.addEventListener("click"'),
    'refresh button should be wired during app setup',
  );
  assert.ok(
    SOURCE.includes('await refreshImageLibrary({ announce: true })'),
    'refresh button should run the image-library rescan path',
  );
  assert.ok(
    refreshSource.includes('availableImages = await fetchImages();'),
    'refresh should re-read the canonical image folder from the API',
  );
  assert.ok(
    refreshSource.includes('selectedImage = refreshedSelection;'),
    'refresh should keep the same selected image only if it still exists',
  );
  assert.ok(
    refreshSource.includes('await syncConfigToServer({ showErrorToast: true });'),
    'refresh should clear the server session when the selected image was removed',
  );
});

test('image library content wheel scrolls the thumbnail strip horizontally', () => {
  const wheelSource = extractFunction('function bindImageLibraryWheelScroll()');

  assert.ok(
    wheelSource.includes('const grid = $("#imageGrid");'),
    'wheel behavior should bind to the image-library content grid, not the title bar/panel',
  );
  assert.ok(
    wheelSource.includes('grid.addEventListener("wheel"'),
    'image library content should listen for wheel events',
  );
  assert.ok(
    wheelSource.includes('grid.scrollWidth <= grid.clientWidth + 1'),
    'wheel handler should only hijack wheel input when horizontal overflow exists',
  );
  assert.ok(
    wheelSource.includes('grid.scrollLeft += delta'),
    'vertical wheel movement should move the thumbnail strip horizontally',
  );
  assert.ok(
    wheelSource.includes('e.preventDefault()'),
    'wheel handler should prevent page scroll only when it consumes the event',
  );
  assert.ok(
    wheelSource.includes('{ passive: false }'),
    'wheel listener must be non-passive so preventDefault works',
  );
  assert.ok(
    SOURCE.includes('bindImageLibraryWheelScroll();'),
    'image library wheel binding should be installed during app setup',
  );
});
