import { validateGuideDefinitions } from "./core/schema.js?v=2026-08-12-guide-companion-v4";
import { GUIDED_SETUP_GUIDE } from "./definitions/guided-setup/guide.js?v=2026-08-11-print-setup-v1";
import {
  BASICS_GUIDE,
  BASICS_LAUNCH_ALIASES,
  BASICS_ROUTE_VIEWS,
} from "./definitions/prisma-generator-basics/guide.js?v=2026-08-12-guide-companion-v4";
import {
  GUIDED_SETUP_HELP_POINTER,
  INTERFACE_PREVIEW_GUIDE,
} from "./definitions/interface-preview/guide.js";
import { SAVING_AND_LOADING_GUIDE } from "./definitions/saving-and-loading/guide.js?v=2026-08-11-print-setup-v1";
import { SETTINGS_DRAWER_GUIDE } from "./definitions/settings-drawer/guide.js?v=2026-08-12-guide-companion-v4";

// Every canonical guide contributes one registry record. Multi-route guides
// supply route views and aliases here without requiring a guide-specific branch
// in route resolution or the state machine.
const GUIDE_REGISTRATIONS = Object.freeze([
  Object.freeze({
    definition: GUIDED_SETUP_GUIDE,
    launches: Object.freeze({ "guided-setup": "full" }),
    route_views: Object.freeze({}),
  }),
  Object.freeze({
    definition: BASICS_GUIDE,
    launches: BASICS_LAUNCH_ALIASES,
    route_views: BASICS_ROUTE_VIEWS,
  }),
  Object.freeze({
    definition: GUIDED_SETUP_HELP_POINTER,
    launches: Object.freeze({ "guided-setup-help-pointer": "full" }),
    route_views: Object.freeze({}),
  }),
  Object.freeze({
    definition: INTERFACE_PREVIEW_GUIDE,
    launches: Object.freeze({ "interface-preview": "full" }),
    route_views: Object.freeze({}),
  }),
  Object.freeze({
    definition: SAVING_AND_LOADING_GUIDE,
    launches: Object.freeze({ "saving-and-loading": "full" }),
    route_views: Object.freeze({}),
  }),
  Object.freeze({
    definition: SETTINGS_DRAWER_GUIDE,
    launches: Object.freeze({ "settings-drawer": "full" }),
    route_views: Object.freeze({}),
  }),
]);

const CANONICAL_GUIDES = Object.freeze(
  GUIDE_REGISTRATIONS.map(registration => registration.definition),
);
const ROUTE_VIEWS_BY_GUIDE = new Map(GUIDE_REGISTRATIONS.map(registration => [
  registration.definition.id,
  new Map(Object.entries(registration.route_views)),
]));
const LAUNCHES = new Map(GUIDE_REGISTRATIONS.flatMap(registration => (
  Object.entries(registration.launches).map(([launchId, routeId]) => [
    launchId,
    Object.freeze({ guide_id: registration.definition.id, route_id: routeId }),
  ])
)));

const KNOWN_GUIDE_ASSETS = new Set(["bubba-blanket"]);

function resolveDefinition(record, canonicalById) {
  const canonical = canonicalById.get(record.guide_id) || null;
  if (!canonical || record.route_id === "full") return canonical;
  return ROUTE_VIEWS_BY_GUIDE.get(record.guide_id)?.get(record.route_id) || null;
}

function validateRegistry(app, launchDefinitions) {
  validateGuideDefinitions(launchDefinitions, {
    actionDefinitions: app.commands.guideActionDefinitions || null,
  });
  for (const guide of launchDefinitions) {
    for (const assetId of guide.baseline?.guide_assets || []) {
      if (!KNOWN_GUIDE_ASSETS.has(assetId)) {
        throw new Error(`Guide ${guide.id} references unknown asset ${assetId}`);
      }
    }
  }
  app.commands.validateGuideTargetRegistry?.(launchDefinitions);
  return true;
}

/** Install canonical guides plus the stable launch-ID-to-route registry. */
export function installFeaturesGuidesRegistry(app) {
  const canonicalById = new Map(CANONICAL_GUIDES.map(guide => [guide.id, guide]));
  const launchDefinitions = Object.freeze([...LAUNCHES.values()].map(record => {
    const definition = resolveDefinition(record, canonicalById);
    if (!definition) throw new Error(`Guide launch points to an unknown route: ${record.guide_id}/${record.route_id}`);
    return definition;
  }));
  validateRegistry(app, launchDefinitions);

  function resolveGuideLaunch(launchId) {
    return LAUNCHES.get(launchId) || null;
  }

  function getGuideDefinition(launchId) {
    const record = resolveGuideLaunch(launchId);
    return record ? resolveDefinition(record, canonicalById) : null;
  }

  function getGuideStep(guide, stepId) {
    return [
      ...(guide?.steps || []),
      ...(guide?.detours || []).flatMap(current => current.steps),
    ].find(current => current.id === stepId) || null;
  }

  function getGuideDetour(guide, detourId) {
    return guide?.detours?.find(current => current.id === detourId) || null;
  }

  function getAllGuideSteps(guide) {
    return Object.freeze([
      ...(guide?.steps || []),
      ...(guide?.detours || []).flatMap(current => current.steps),
    ]);
  }

  function getCatalogGuides() {
    return Object.freeze(launchDefinitions
      .filter(guide => !guide.internal && guide.catalog)
      .sort((left, right) => (
        left.catalog.group.localeCompare(right.catalog.group)
        || left.catalog.order - right.catalog.order
      )));
  }

  Object.assign(app.commands, {
    getGuideDefinition,
    getGuideDetour,
    getGuideStep,
    getAllGuideSteps,
    getCatalogGuides,
    resolveGuideLaunch,
    validateGuideDefinitions: () => validateRegistry(app, launchDefinitions),
  });
  app.state.guides.definitions = CANONICAL_GUIDES;
  app.state.guides.launches = LAUNCHES;
}
