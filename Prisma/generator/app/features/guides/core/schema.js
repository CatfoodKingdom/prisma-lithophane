const GUIDE_OVERLAY_MODES = new Set(["spotlight", "full-scrim"]);
const WORKSPACE_POLICIES = new Set(["non-destructive", "basic-teaching", "preview"]);
const COMPLETION_KINDS = new Set(["manual", "event", "interaction"]);
const GUIDE_PLACEMENTS = new Set(["right", "bottom", "left", "top"]);
const GUIDE_VIEWPORT_ANCHORS = new Set(["center", "center-left", "center-right"]);
const GUIDE_CARD_SIZES = new Set(["wide"]);
const GUIDE_COMPANION_LAYOUTS = new Set(["single", "row", "column", "grid"]);
const GUIDE_COMPANION_ITEM_TYPES = new Set(["image", "text"]);
const GUIDE_COMPANION_SOURCE_PREFIX = "/assets/guides/";
const STABLE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const RUNTIME_PATH_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9][A-Za-z0-9_-]*)*$/;
const GUIDE_PARTICIPATING_SURFACES = new Set([
  "#appDialog", "#deckCardMenu", "#opProgress", "#paletteSaveModal",
  "#savedRunsModal", "#settingsProfileModal", "#settingsProfileSaveModal",
]);

export const GUIDE_DURABLE_OPERATIONS = Object.freeze({
  "palette.saved.create": Object.freeze({
    deck_card_id: "string", palette_signature: "array", name: "string",
  }),
  "palette.saved.delete": Object.freeze({ saved_record_id: "string" }),
  "settings.profile.create": Object.freeze({
    name: "string", source_profile_id: "string", t_max: "number",
  }),
  "settings.profile.overwrite": Object.freeze({ profile_id: "string", name: "string" }),
  "settings.profile.delete": Object.freeze({ profile_id: "string" }),
  "settings.profile.set-startup": Object.freeze({ profile_id: "string" }),
  "settings.profile.restore-system": Object.freeze({}),
  "solve.saved-run.create": Object.freeze({ run_card_id: "string", label: "string" }),
  "solve.saved-run.delete": Object.freeze({ save_id: "string" }),
  "solve.saved-run.rename": Object.freeze({ save_id: "string", label: "string" }),
  "solve.saved-run.promote": Object.freeze({ save_id: "string" }),
  "solve.auto-run.delete": Object.freeze({ save_id: "string" }),
  "solve.saved-run.upload": Object.freeze({}),
});

/** @typedef {"non-destructive"|"basic-teaching"|"preview"} GuideWorkspacePolicy */

export function completion(kind, options = {}) {
  return Object.freeze({ kind, ...options });
}

function assertExactKeys(value, allowed, label) {
  const unknown = Object.keys(value || {}).filter(key => !allowed.has(key));
  if (unknown.length) throw new Error(`${label} contains unknown fields: ${unknown.join(", ")}`);
}

function normalizedOptionalText(value, label) {
  if (value == null) return null;
  if (typeof value !== "string" || !value.trim()) throw new Error(`${label} must be nonblank text`);
  return value.trim();
}

function normalizeCompanionSource(value) {
  if (typeof value !== "string" || !value.startsWith(GUIDE_COMPANION_SOURCE_PREFIX)) {
    throw new Error(`Guide companion image source must begin with ${GUIDE_COMPANION_SOURCE_PREFIX}`);
  }
  if (value.includes("\\") || value.includes("?") || value.includes("#") || value.includes("%")) {
    throw new Error("Guide companion image source contains unsupported URL syntax");
  }
  const segments = value.split("/").slice(1);
  if (segments.some(segment => !segment || segment === "." || segment === "..")) {
    throw new Error("Guide companion image source contains an invalid path segment");
  }
  return value;
}

function normalizeCompanion(companion) {
  if (companion == null) return null;
  if (!companion || typeof companion !== "object" || Array.isArray(companion)) {
    throw new Error("Guide companion must be an object");
  }
  assertExactKeys(
    companion,
    new Set(["title", "layout", "preferredPlacements", "items"]),
    "Guide companion",
  );
  if (!GUIDE_COMPANION_LAYOUTS.has(companion.layout)) {
    throw new Error("Guide companion has an invalid layout");
  }
  if (!Array.isArray(companion.items) || companion.items.length < 1 || companion.items.length > 8) {
    throw new Error("Guide companion must contain 1-8 items");
  }
  if (companion.layout === "single" && companion.items.length !== 1) {
    throw new Error("Guide companion single layout requires exactly one item");
  }
  if (companion.layout !== "single" && companion.items.length < 2) {
    throw new Error(`Guide companion ${companion.layout} layout requires at least two items`);
  }
  const placements = companion.preferredPlacements == null
    ? ["right", "left", "bottom", "top"]
    : companion.preferredPlacements;
  if (
    !Array.isArray(placements)
    || placements.length !== GUIDE_PLACEMENTS.size
    || new Set(placements).size !== GUIDE_PLACEMENTS.size
    || placements.some(placement => !GUIDE_PLACEMENTS.has(placement))
  ) {
    throw new Error("Guide companion placements must contain right, left, bottom, and top exactly once");
  }
  const items = companion.items.map((item, index) => {
    if (!item || typeof item !== "object" || Array.isArray(item)
      || !GUIDE_COMPANION_ITEM_TYPES.has(item.type)) {
      throw new Error(`Guide companion item ${index + 1} has an invalid type`);
    }
    if (item.type === "text") {
      assertExactKeys(item, new Set(["type", "text"]), `Guide companion text item ${index + 1}`);
      const text = normalizedOptionalText(item.text, `Guide companion text item ${index + 1}`);
      if (text == null) throw new Error(`Guide companion text item ${index + 1} must be nonblank text`);
      return Object.freeze({
        type: "text",
        text,
      });
    }
    assertExactKeys(
      item,
      new Set(["type", "src", "alt", "caption", "lightboxTitle", "expandable"]),
      `Guide companion image item ${index + 1}`,
    );
    if (item.expandable != null && typeof item.expandable !== "boolean") {
      throw new Error(`Guide companion image item ${index + 1} expandable must be boolean`);
    }
    return Object.freeze({
      type: "image",
      src: normalizeCompanionSource(item.src),
      expandable: item.expandable !== false,
      ...(normalizedOptionalText(item.alt, `Guide companion image item ${index + 1} alt`) != null
        ? { alt: item.alt.trim() }
        : {}),
      ...(normalizedOptionalText(item.caption, `Guide companion image item ${index + 1} caption`) != null
        ? { caption: item.caption.trim() }
        : {}),
      ...(normalizedOptionalText(item.lightboxTitle, `Guide companion image item ${index + 1} lightbox title`) != null
        ? { lightbox_title: item.lightboxTitle.trim() }
        : {}),
    });
  });
  return Object.freeze({
    layout: companion.layout,
    preferred_placements: Object.freeze([...placements]),
    items: Object.freeze(items),
    ...(normalizedOptionalText(companion.title, "Guide companion title") != null
      ? { title: companion.title.trim() }
      : {}),
  });
}

/** Build an immutable guide step from the author-facing representation. */
export function step({
  id,
  title,
  body,
  target,
  reveal = target,
  overlayMode = "spotlight",
  placementGroup = null,
  placements = ["right", "bottom", "left", "top"],
  viewportAnchor = null,
  completionMode = completion("manual"),
  allowPrevious = true,
  allowSkip = true,
  allowEnd = true,
  nextLabel = null,
  detourLayout = null,
  cardSize = null,
  followup = null,
  note = null,
  visual = null,
  companion = null,
  participatingSurfaces = [],
  enterActions = [],
  completeActions = [],
}) {
  return Object.freeze({
    id,
    title,
    body,
    target_id: target,
    reveal_id: reveal,
    overlay_mode: overlayMode,
    preferred_placements: Object.freeze([...placements]),
    ...(placementGroup ? { placement_group: placementGroup } : {}),
    ...(viewportAnchor ? { viewport_anchor: viewportAnchor } : {}),
    completion: completionMode,
    allow_previous: allowPrevious,
    allow_skip: allowSkip,
    ...(allowEnd === false ? { allow_end: false } : {}),
    ...(nextLabel ? { next_label: nextLabel } : {}),
    ...(detourLayout ? { detour_layout: detourLayout } : {}),
    ...(cardSize ? { card_size: cardSize } : {}),
    ...(followup ? { followup: Object.freeze({ ...followup }) } : {}),
    ...(note ? { note: Object.freeze({ ...note }) } : {}),
    ...(visual ? { visual: Object.freeze({ ...visual }) } : {}),
    ...(companion == null ? {} : { companion: normalizeCompanion(companion) }),
    ...(participatingSurfaces.length
      ? { participating_surfaces: Object.freeze([...participatingSurfaces]) }
      : {}),
    enter_actions: Object.freeze(enterActions.map(action => Object.freeze({ ...action }))),
    complete_actions: Object.freeze(completeActions.map(action => Object.freeze({ ...action }))),
  });
}

export function chapter(id, label, chapterSteps) {
  return Object.freeze({
    id,
    label,
    step_ids: Object.freeze(chapterSteps.map(current => current.id)),
  });
}

/**
 * Optional depth attached to a spine step.
 *
 * The routing policies all default to the behavior that shipped before they
 * existed, so a detour that declares none of them navigates exactly as it did
 * before: offered once, no Previous on its first step, "Exit Detour" everywhere
 * but the final step.
 */
export function detour({
  id,
  label,
  description,
  offerStepId,
  returnStepId,
  returnPredicateId = null,
  repeatable = false,
  previousReturnsToOffer = false,
  suppressPreviousOnReturn = false,
  exitLabel = null,
  allowExitOnFinal = false,
  buttonLabel = null,
  showStatus = true,
  detourSteps,
}) {
  return Object.freeze({
    id,
    label,
    description,
    offer_step_id: offerStepId,
    return_step_id: returnStepId,
    return_predicate_id: returnPredicateId,
    repeatable,
    previous_returns_to_offer: previousReturnsToOffer,
    suppress_previous_on_return: suppressPreviousOnReturn,
    exit_label: exitLabel,
    allow_exit_on_final: allowExitOnFinal,
    ...(buttonLabel ? { button_label: buttonLabel } : {}),
    ...(showStatus === false ? { show_status: false } : {}),
    steps: Object.freeze(detourSteps),
  });
}

export function freezeGuide(definition) {
  return Object.freeze({ ...definition });
}

function validateActions(guide, current, actionDefinitions) {
  for (const [phase, descriptors] of [
    ["enter", current.enter_actions || []],
    ["complete", current.complete_actions || []],
  ]) {
    for (const descriptor of descriptors) {
      if (!descriptor?.action || typeof descriptor.action !== "string") {
        throw new Error(`Guide ${guide.id} step ${current.id} has an invalid action descriptor`);
      }
      if (
        descriptor.result_key != null
        && (
          typeof descriptor.result_key !== "string"
          || !STABLE_ID_PATTERN.test(descriptor.result_key)
        )
      ) {
        throw new Error(`Guide ${guide.id} step ${current.id} has an invalid action result key`);
      }
      if (descriptor.ensure_on_entry != null && typeof descriptor.ensure_on_entry !== "boolean") {
        throw new Error(`Guide ${guide.id} step ${current.id} has an invalid ensure-on-entry policy`);
      }
      if (actionDefinitions && !actionDefinitions.has(descriptor.action)) {
        throw new Error(`Guide ${guide.id} step ${current.id} references unknown action ${descriptor.action}`);
      }
      const definition = actionDefinitions?.get(descriptor.action);
      if (definition && !definition.workspacePolicies.includes(guide.workspace_policy)) {
        throw new Error(`Guide ${guide.id} step ${current.id} uses an incompatible action ${descriptor.action}`);
      }
      if (definition && !definition.executionTiming.includes(phase)) {
        throw new Error(`Guide ${guide.id} step ${current.id} uses ${descriptor.action} at an incompatible time`);
      }
      definition?.validate(descriptor.input || {});
      if (descriptor.ensure_on_entry && !definition?.idempotent) {
        throw new Error(`Guide ${guide.id} step ${current.id} ensures a non-idempotent action on entry`);
      }
    }
  }
  if ((current.complete_actions || []).some(descriptor => descriptor.ensure_on_entry)) {
    throw new Error(`Guide ${guide.id} step ${current.id} uses ensure_on_entry on completion`);
  }
}

function validateGuide(guide, actionDefinitions) {
  if (!STABLE_ID_PATTERN.test(guide.id || "")) {
    throw new Error(`Guide has an invalid stable id: ${guide.id}`);
  }
  if (
    typeof guide.title !== "string"
    || !guide.title.trim()
    || typeof guide.summary !== "string"
    || !guide.summary.trim()
  ) {
    throw new Error(`Guide ${guide.id} is missing user-facing metadata`);
  }
  if (!Number.isInteger(guide.version) || guide.version < 1) {
    throw new Error(`Guide ${guide.id} has an invalid version`);
  }
  if (!Array.isArray(guide.steps) || guide.steps.length === 0) {
    throw new Error(`Guide ${guide.id} has no steps`);
  }
  if (guide.catalog && (!guide.catalog.group || !Number.isFinite(guide.catalog.order))) {
    throw new Error(`Guide ${guide.id} has incomplete catalog metadata`);
  }
  if (!WORKSPACE_POLICIES.has(guide.workspace_policy)) {
    throw new Error(`Guide ${guide.id} has an invalid workspace policy`);
  }
  if (guide.workspace_policy === "basic-teaching" && !guide.baseline) {
    throw new Error(`Guide ${guide.id} is missing its teaching baseline`);
  }
  for (const [timing, descriptors] of [
    ["preflight", guide.preflight_actions || []],
    ["preparation", guide.preparation_actions || []],
  ]) for (const descriptor of descriptors) {
    if (!descriptor?.action || !actionDefinitions?.has(descriptor.action)) {
      throw new Error(`Guide ${guide.id} has an invalid ${timing} action`);
    }
    const action = actionDefinitions.get(descriptor.action);
    if (
      descriptor.result_key != null
      && (
        typeof descriptor.result_key !== "string"
        || !STABLE_ID_PATTERN.test(descriptor.result_key)
      )
    ) {
      throw new Error(`Guide ${guide.id} has an invalid ${timing} result key`);
    }
    if (descriptor.ensure_on_entry != null) {
      throw new Error(`Guide ${guide.id} uses ensure_on_entry during ${timing}`);
    }
    if (!action.workspacePolicies.includes(guide.workspace_policy)
      || !action.executionTiming.includes(timing)) {
      throw new Error(`Guide ${guide.id} has an incompatible ${timing} action ${descriptor.action}`);
    }
    if (timing === "preflight" && (action.resourceDisposition !== "none" || !action.idempotent)) {
      throw new Error(`Guide ${guide.id} preflight action ${descriptor.action} must be read-only and idempotent`);
    }
    action.validate(descriptor.input || {});
  }

  if (guide.text_substitutions != null) {
    if (!guide.text_substitutions || typeof guide.text_substitutions !== "object" || Array.isArray(guide.text_substitutions)) {
      throw new Error(`Guide ${guide.id} has invalid text substitutions`);
    }
    for (const [token, path] of Object.entries(guide.text_substitutions)) {
      if (!/^\{\{[A-Za-z][A-Za-z0-9]*\}\}$/.test(token)
        || typeof path !== "string"
        || !RUNTIME_PATH_PATTERN.test(path)) {
        throw new Error(`Guide ${guide.id} has an invalid text substitution`);
      }
    }
  }

  if (guide.durable_mutation_policy != null) {
    const policy = guide.durable_mutation_policy;
    if (!policy || policy.default !== "deny" || !policy.steps || typeof policy.steps !== "object") {
      throw new Error(`Guide ${guide.id} has an invalid durable mutation policy`);
    }
    const knownStepIds = new Set([
      ...guide.steps.map(current => current.id),
      ...(guide.detours || []).flatMap(current => current.steps.map(item => item.id)),
    ]);
    for (const [stepId, rules] of Object.entries(policy.steps)) {
      if (!knownStepIds.has(stepId) || !Array.isArray(rules) || !rules.length) {
        throw new Error(`Guide ${guide.id} has an invalid durable mutation step ${stepId}`);
      }
      for (const rule of rules) {
        const detailSchema = GUIDE_DURABLE_OPERATIONS[rule?.operation];
        if (!detailSchema || !rule.match || typeof rule.match !== "object" || Array.isArray(rule.match)) {
          throw new Error(`Guide ${guide.id} has an invalid durable mutation rule`);
        }
        if (Object.keys(rule.match).length !== Object.keys(detailSchema).length) {
          throw new Error(`Guide ${guide.id} durable rule ${rule.operation} must match every detail field`);
        }
        for (const [key, matcher] of Object.entries(rule.match)) {
          if (!(key in detailSchema) || !matcher || typeof matcher !== "object" || Array.isArray(matcher)) {
            throw new Error(`Guide ${guide.id} durable rule ${rule.operation} has an invalid detail matcher`);
          }
          const matcherKeys = Object.keys(matcher);
          if (matcherKeys.length !== 1 || !["literal", "context"].includes(matcherKeys[0])) {
            throw new Error(`Guide ${guide.id} durable rule ${rule.operation} has an invalid matcher`);
          }
          if ("context" in matcher && (typeof matcher.context !== "string" || !RUNTIME_PATH_PATTERN.test(matcher.context))) {
            throw new Error(`Guide ${guide.id} durable rule ${rule.operation} has an invalid runtime path`);
          }
          if ("literal" in matcher) {
            const value = matcher.literal;
            const expected = detailSchema[key];
            const valid = expected === "array"
              ? Array.isArray(value) && value.every(item => ["string", "number", "boolean"].includes(typeof item))
              : typeof value === expected;
            if (!valid) throw new Error(`Guide ${guide.id} durable rule ${rule.operation} has a type-incompatible literal`);
          }
        }
      }
    }
  }

  const detourIds = new Set();
  const detourSteps = [];
  const detoursByOfferStep = new Map();
  for (const currentDetour of guide.detours || []) {
    if (!currentDetour.id || detourIds.has(currentDetour.id)) {
      throw new Error(`Guide ${guide.id} has a duplicate or missing detour id`);
    }
    detourIds.add(currentDetour.id);
    if (
      !currentDetour.label
      || !currentDetour.description
      || !currentDetour.offer_step_id
      || !currentDetour.return_step_id
      || !Array.isArray(currentDetour.steps)
      || currentDetour.steps.length === 0
    ) {
      throw new Error(`Guide ${guide.id} detour ${currentDetour.id} is incomplete`);
    }
    for (const policy of [
      "repeatable",
      "previous_returns_to_offer",
      "suppress_previous_on_return",
      "allow_exit_on_final",
      "show_status",
    ]) {
      if (currentDetour[policy] != null && typeof currentDetour[policy] !== "boolean") {
        throw new Error(`Guide ${guide.id} detour ${currentDetour.id} has an invalid ${policy} policy`);
      }
    }
    if (
      currentDetour.exit_label != null
      && (typeof currentDetour.exit_label !== "string" || !currentDetour.exit_label.trim())
    ) {
      throw new Error(`Guide ${guide.id} detour ${currentDetour.id} has an invalid exit label`);
    }
    if (
      !guide.steps.some(current => current.id === currentDetour.offer_step_id)
      || !guide.steps.some(current => current.id === currentDetour.return_step_id)
    ) {
      throw new Error(`Guide ${guide.id} detour ${currentDetour.id} references an unknown spine step`);
    }
    if (currentDetour.steps.some(current => current.detour)) {
      throw new Error(`Guide ${guide.id} detour ${currentDetour.id} contains a nested detour`);
    }
    detoursByOfferStep.set(
      currentDetour.offer_step_id,
      (detoursByOfferStep.get(currentDetour.offer_step_id) || 0) + 1,
    );
    detourSteps.push(...currentDetour.steps);
  }
  // A step offering several routes is a menu. There is no defined layout for
  // rendering a list of entry controls beside a spotlighted control, so the
  // offering step has to be targetless.
  for (const [offerStepId, offered] of detoursByOfferStep) {
    if (offered < 2) continue;
    const offerStep = guide.steps.find(current => current.id === offerStepId);
    if (offerStep.target_id !== null) {
      throw new Error(`Guide ${guide.id} step ${offerStepId} offers several detours beside a target`);
    }
  }

  const stepIds = new Set();
  for (const current of [...guide.steps, ...detourSteps]) {
    if (!STABLE_ID_PATTERN.test(current.id || "") || stepIds.has(current.id)) {
      throw new Error(`Guide ${guide.id} has a duplicate step id or an invalid step id`);
    }
    stepIds.add(current.id);
    if (
      typeof current.title !== "string"
      || !current.title.trim()
      || typeof current.body !== "string"
      || !current.body.trim()
    ) {
      throw new Error(`Guide ${guide.id} step ${current.id} is missing user-facing text`);
    }
    if (
      typeof current.allow_previous !== "boolean"
      || typeof current.allow_skip !== "boolean"
      || (current.allow_end != null && typeof current.allow_end !== "boolean")
      || !Array.isArray(current.preferred_placements)
      || current.preferred_placements.some(placement => !GUIDE_PLACEMENTS.has(placement))
      || (current.viewport_anchor != null && !GUIDE_VIEWPORT_ANCHORS.has(current.viewport_anchor))
    ) {
      throw new Error(`Guide ${guide.id} step ${current.id} has invalid navigation or placement metadata`);
    }
    if (current.detour_layout != null && current.detour_layout !== "button-description") {
      throw new Error(`Guide ${guide.id} step ${current.id} has an invalid detour layout`);
    }
    if (current.card_size != null && !GUIDE_CARD_SIZES.has(current.card_size)) {
      throw new Error(`Guide ${guide.id} step ${current.id} has an invalid card size`);
    }
    if (!Array.isArray(current.participating_surfaces || [])
      || (current.participating_surfaces || []).some(selector => !GUIDE_PARTICIPATING_SURFACES.has(selector))) {
      throw new Error(`Guide ${guide.id} step ${current.id} has invalid participating surfaces`);
    }
    const targetless = current.target_id === null;
    if ((!targetless && (!current.target_id || !current.reveal_id)) || !current.completion?.kind) {
      throw new Error(`Guide ${guide.id} step ${current.id} is incomplete`);
    }
    if (!GUIDE_OVERLAY_MODES.has(current.overlay_mode)) {
      throw new Error(`Guide ${guide.id} step ${current.id} has an invalid overlay mode`);
    }
    if (!COMPLETION_KINDS.has(current.completion.kind)) {
      throw new Error(`Guide ${guide.id} step ${current.id} has an invalid completion kind`);
    }
    if (current.completion.kind === "event") {
      const events = current.completion.events || (current.completion.event ? [current.completion.event] : []);
      if (
        !Array.isArray(events)
        || events.length === 0
        || events.some(event => typeof event !== "string" || !event.trim())
        || !current.completion.predicate_id
      ) {
        throw new Error(`Guide ${guide.id} step ${current.id} has an incomplete event contract`);
      }
    }
    if (current.completion.accept_preexisting && current.completion.kind !== "event") {
      throw new Error(`Guide ${guide.id} step ${current.id} accepts preexisting state without an event contract`);
    }
    if (
      typeof current.completion.auto_advance !== "undefined"
      && (
        typeof current.completion.auto_advance !== "boolean"
        || current.completion.kind !== "event"
      )
    ) {
      throw new Error(`Guide ${guide.id} step ${current.id} has an invalid auto-advance policy`);
    }
    if (current.followup && (!current.followup.guide_id || !current.followup.text || !current.followup.label)) {
      throw new Error(`Guide ${guide.id} step ${current.id} has an incomplete follow-up action`);
    }
    if (current.note && (!current.note.label || !current.note.text)) {
      throw new Error(`Guide ${guide.id} step ${current.id} has an incomplete note`);
    }
    validateActions(guide, current, actionDefinitions);
  }

  if (!Array.isArray(guide.chapters) || guide.chapters.length === 0) {
    throw new Error(`Guide ${guide.id} has no chapter metadata`);
  }
  const chapterIds = new Set();
  const chapterStepIds = [];
  for (const currentChapter of guide.chapters) {
    if (
      !STABLE_ID_PATTERN.test(currentChapter.id || "")
      || chapterIds.has(currentChapter.id)
      || typeof currentChapter.label !== "string"
      || !currentChapter.label.trim()
    ) {
      throw new Error(`Guide ${guide.id} has invalid chapter metadata`);
    }
    chapterIds.add(currentChapter.id);
    if (!Array.isArray(currentChapter.step_ids) || currentChapter.step_ids.length === 0) {
      throw new Error(`Guide ${guide.id} chapter ${currentChapter.id} has no steps`);
    }
    chapterStepIds.push(...currentChapter.step_ids);
  }
  const spineIds = guide.steps.map(current => current.id);
  if (
    chapterStepIds.length !== spineIds.length
    || new Set(chapterStepIds).size !== chapterStepIds.length
    || spineIds.some(id => !chapterStepIds.includes(id))
  ) {
    throw new Error(`Guide ${guide.id} chapters do not cover the spine exactly once`);
  }
}

/** Validate the complete launch surface, including aliases and follow-ups. */
export function validateGuideDefinitions(definitions, {
  aliases = new Map(),
  actionDefinitions = null,
} = {}) {
  const guideIds = new Set();
  for (const guide of definitions) {
    if (!guide.id || guideIds.has(guide.id)) throw new Error(`Duplicate or missing guide id: ${guide.id}`);
    guideIds.add(guide.id);
    validateGuide(guide, actionDefinitions);
  }
  for (const [alias, destination] of aliases) {
    if (!alias || guideIds.has(alias) || !guideIds.has(destination)) {
      throw new Error(`Invalid guide launch alias: ${alias}`);
    }
  }
  for (const guide of definitions) {
    for (const current of [...guide.steps, ...(guide.detours || []).flatMap(item => item.steps)]) {
      if (current.followup && !guideIds.has(current.followup.guide_id) && !aliases.has(current.followup.guide_id)) {
        throw new Error(`Guide ${guide.id} step ${current.id} references an unknown follow-up guide`);
      }
    }
  }
  return true;
}
