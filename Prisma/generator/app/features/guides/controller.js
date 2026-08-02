import { createAnchoredMenuController } from "../../core/anchored-menu.js";

const FIRST_RUN_GUIDE_OFFER_ENABLED = true;
const GUIDE_STATE_SCHEMA_VERSION = 2;
const WELCOME_STATUSES = new Set(["not_offered", "deferred", "declined", "accepted"]);

function clone(value) {
  return typeof structuredClone === "function"
    ? structuredClone(value)
    : JSON.parse(JSON.stringify(value));
}

/** Install first-launch state, guide state-machine, menu, catalog, and navigation. */
export function installFeaturesGuidesController(app) {
  let initialized = false;
  let menuController = null;
  let completionDisposer = null;
  let stateBeforeSuspension = null;
  let catalogRestoreFocus = null;
  let controllerDocument = null;
  let firstLaunchWaitDisposer = null;
  let firstLaunchOfferStarted = false;
  let lastWelcomePersistenceFailure = null;
  let runtimeCleanupPromise = Promise.resolve();
  let pendingAdvanceAfterSuspension = false;

  function guideIsActive() {
    return !!app.state.guides.currentGuide
      && !["idle", "completed"].includes(app.state.guides.runtimeState);
  }

  function activeDetourDefinition(guide = app.state.guides.currentGuide) {
    const detourId = app.state.guides.activeDetour?.id;
    return detourId ? app.commands.getGuideDetour(guide, detourId) : null;
  }

  function activeRouteSteps(guide = app.state.guides.currentGuide) {
    return activeDetourDefinition(guide)?.steps || guide?.steps || [];
  }

  function guideRenderPayload(guide, step) {
    const route = activeRouteSteps(guide);
    return {
      guide,
      step,
      stepIndex: route.indexOf(step),
      stepCount: route.length,
      routeKind: activeDetourDefinition(guide) ? "detour" : "spine",
    };
  }

  function getOfferedGuideDetour() {
    if (app.state.guides.activeDetour) return null;
    const guide = app.state.guides.currentGuide;
    const stepId = app.state.guides.currentStep?.id;
    return guide?.detours?.find(current => current.offer_step_id === stepId) || null;
  }

  function getGuideProgressModel() {
    const guide = app.state.guides.currentGuide;
    const step = app.state.guides.currentStep;
    if (!guide || !step) return null;
    const currentDetour = activeDetourDefinition(guide);
    if (currentDetour) {
      const index = currentDetour.steps.indexOf(step);
      return {
        label: currentDetour.label,
        count: `Optional · ${index + 1} of ${currentDetour.steps.length}`,
        value: index + 1,
        max: currentDetour.steps.length,
        valueText: `${currentDetour.label}, optional step ${index + 1} of ${currentDetour.steps.length}`,
        mode: "detour",
        segments: [{
          id: currentDetour.id,
          label: currentDetour.label,
          state: "active",
          fraction: (index + 1) / currentDetour.steps.length,
        }],
      };
    }
    const spineIndex = guide.steps.indexOf(step);
    const chapterIndex = guide.chapters.findIndex(current => current.step_ids.includes(step.id));
    const currentChapter = guide.chapters[chapterIndex];
    const chapterStepIndex = currentChapter.step_ids.indexOf(step.id);
    return {
      label: currentChapter.label,
      count: `${chapterStepIndex + 1} of ${currentChapter.step_ids.length}`,
      value: spineIndex + 1,
      max: guide.steps.length,
      valueText: `${currentChapter.label}, step ${chapterStepIndex + 1} of ${currentChapter.step_ids.length}; required step ${spineIndex + 1} of ${guide.steps.length}`,
      mode: "spine",
      segments: guide.chapters.map((current, index) => ({
        id: current.id,
        label: current.label,
        state: index < chapterIndex ? "complete" : index === chapterIndex ? "active" : "upcoming",
        fraction: index < chapterIndex
          ? 1
          : index === chapterIndex
            ? (chapterStepIndex + 1) / current.step_ids.length
            : 0,
      })),
    };
  }

  function setOnboardingState(state) {
    app.state.guides.onboardingState = state;
  }

  async function loadGuideState() {
    try {
      const state = await app.api.fetchGuideState();
      app.state.guides.onboardingPersistenceAvailable = true;
      setOnboardingState(state);
    } catch {
      app.state.guides.onboardingPersistenceAvailable = false;
    }
    return app.state.guides.onboardingState;
  }

  async function persistWelcomeStatus(welcomeStatus) {
    if (!WELCOME_STATUSES.has(welcomeStatus)) {
      throw new Error(`Unknown first-launch welcome status: ${welcomeStatus}`);
    }
    const current = app.state.guides.onboardingState;
    const draft = clone(current);
    draft.schema_version = GUIDE_STATE_SCHEMA_VERSION;
    draft.revision = current.revision;
    draft.welcome_status = welcomeStatus;
    if (
      !app.state.guides.onboardingPersistenceAvailable
      || typeof app.api.putGuideState !== "function"
    ) {
      lastWelcomePersistenceFailure = "unavailable";
      return false;
    }
    lastWelcomePersistenceFailure = null;
    try {
      const saved = await app.api.putGuideState(draft, current.revision);
      setOnboardingState(saved);
      return true;
    } catch (error) {
      if (error?.status === 409) {
        lastWelcomePersistenceFailure = "conflict";
        try {
          setOnboardingState(await app.api.fetchGuideState());
        } catch { /* retain the last known state */ }
        app.commands.showToast?.(
          "First-launch status changed in another browser tab. Please try again.",
          "warn",
        );
      } else {
        lastWelcomePersistenceFailure = "unavailable";
        app.state.guides.onboardingPersistenceAvailable = false;
        app.commands.showToast?.(
          "Prisma could not save the first-launch choice.",
          "warn",
        );
      }
      return false;
    }
  }

  function modelLibraryRecoveryOpen() {
    const modal = app.state.ui.$("#modelLibrariesModal");
    return !!modal && modal.getAttribute("aria-hidden") === "false";
  }

  async function maybeOfferGuidedSetup() {
    if (!FIRST_RUN_GUIDE_OFFER_ENABLED || firstLaunchOfferStarted) return false;
    const force = !!app.state.guides.forceGuidedSetup;
    const onboarding = app.state.guides.onboardingState;
    if (
      !force
      && (
        !app.state.guides.onboardingPersistenceAvailable
        || onboarding?.welcome_status !== "not_offered"
      )
    ) {
      return false;
    }
    if (modelLibraryRecoveryOpen()) {
      if (!firstLaunchWaitDisposer) {
        firstLaunchWaitDisposer = app.events.subscribe("model-libraries.closed", () => {
          firstLaunchWaitDisposer?.();
          firstLaunchWaitDisposer = null;
          void maybeOfferGuidedSetup();
        });
      }
      return false;
    }

    firstLaunchOfferStarted = true;
    firstLaunchWaitDisposer?.();
    firstLaunchWaitDisposer = null;
    const begin = await (app.commands.appConfirm?.(
      "Would you like to begin a brief guided setup to configure Prisma Generator for your printer and introduce its essential controls?",
      {
        title: "Welcome to Prisma Generator!",
        ok: "Start Guided Setup",
        cancel: "Not Now",
      },
    ) ?? Promise.resolve(false));
    if (!force) {
      const persisted = await persistWelcomeStatus(begin ? "accepted" : "declined");
      if (
        !persisted
        && (
          lastWelcomePersistenceFailure === "conflict"
          || app.state.guides.onboardingState?.welcome_status !== "not_offered"
        )
      ) {
        return false;
      }
    }
    return startGuide(begin ? "guided-setup" : "guided-setup-help-pointer");
  }

  function renderGuidesCatalog() {
    const list = app.state.ui.$("#guidesCatalogList");
    if (!list) return false;
    const documentTarget = list.ownerDocument || controllerDocument;
    if (!documentTarget?.createElement || !list.replaceChildren) return false;
    const groups = new Map();
    for (const guide of app.commands.getCatalogGuides()) {
      if (!groups.has(guide.catalog.group)) groups.set(guide.catalog.group, []);
      groups.get(guide.catalog.group).push(guide);
    }
    const fragments = [];
    for (const [groupLabel, guides] of groups) {
      const group = documentTarget.createElement("section");
      group.className = "guide-catalog-group";
      const heading = documentTarget.createElement("h4");
      heading.className = "guide-catalog-group-title";
      heading.textContent = groupLabel;
      const items = documentTarget.createElement("div");
      items.className = "summary-action-grid";
      for (const guide of guides) {
        const entry = documentTarget.createElement("article");
        entry.className = "summary-action-row";
        const copy = documentTarget.createElement("div");
        copy.className = "summary-action-copy";
        const title = documentTarget.createElement("h5");
        title.className = "summary-action-title";
        title.id = `guideCatalogTitle-${guide.id}`;
        title.textContent = guide.title;
        if (guide.catalog.badge) {
          const badge = documentTarget.createElement("span");
          badge.className = "guide-preview-label";
          badge.textContent = guide.catalog.badge;
          title.append(" ", badge);
        }
        const summary = documentTarget.createElement("p");
        summary.className = "summary-action-description";
        summary.id = `guideCatalogSummary-${guide.id}`;
        summary.textContent = guide.summary;
        copy.append(title, summary);
        if (guide.catalog.note) {
          const note = documentTarget.createElement("p");
          note.className = "summary-action-note";
          note.textContent = guide.catalog.note;
          copy.appendChild(note);
        }
        const button = documentTarget.createElement("button");
        button.className = "primary-button small";
        button.type = "button";
        button.dataset.guideId = guide.id;
        button.textContent = "Start";
        button.setAttribute("aria-label", `Start ${guide.title}`);
        button.setAttribute("aria-describedby", summary.id);
        entry.append(copy, button);
        items.appendChild(entry);
      }
      group.append(heading, items);
      fragments.push(group);
    }
    list.replaceChildren(...fragments);
    return true;
  }

  async function openGuidesCatalog() {
    const modal = app.state.ui.$("#guidesCatalogModal");
    if (!modal) return false;
    if (guideIsActive()) {
      const confirmed = await (app.commands.appConfirm?.(
        "End the current guide and open the Guides Library?",
        { title: "Open Guides Library", ok: "End Guide", cancel: "Keep Guide Open" },
      ) ?? Promise.resolve(false));
      if (!confirmed) return false;
      endGuide({ restoreFocus: false });
    }
    renderGuidesCatalog();
    catalogRestoreFocus = app.state.ui.$("#helpGuidesMenuBtn");
    modal.classList.remove("is-hidden");
    modal.setAttribute("aria-hidden", "false");
    app.state.ui.$("#guidesCatalogClose")?.focus();
    return true;
  }

  function handleCatalogAction(event) {
    const button = event.target?.closest?.("[data-guide-id]");
    if (!button) return;
    void startGuide(button.dataset.guideId);
  }

  function closeGuidesCatalog({ restoreFocus = true } = {}) {
    const modal = app.state.ui.$("#guidesCatalogModal");
    if (!modal || modal.getAttribute("aria-hidden") !== "false") return;
    modal.classList.add("is-hidden");
    modal.setAttribute("aria-hidden", "true");
    if (restoreFocus) (catalogRestoreFocus || app.state.ui.$("#helpGuidesMenuBtn"))?.focus?.();
    catalogRestoreFocus = null;
  }

  function cleanupCompletionSubscription() {
    completionDisposer?.();
    completionDisposer = null;
  }

  function restorePrototypePresentation() {
    const guide = app.state.guides.currentGuide;
    if (guide?.restore_presentation) {
      app.commands.restoreGuidePresentation(app.state.guides.presentationSnapshot);
    }
    app.state.guides.presentationSnapshot = null;
  }

  function setGuideSuspended(suspended) {
    if (suspended) {
      if (app.state.guides.runtimeState === "suspended_by_dialog") return;
      stateBeforeSuspension = app.state.guides.runtimeState;
      app.state.guides.runtimeState = "suspended_by_dialog";
      return;
    }
    if (app.state.guides.runtimeState === "suspended_by_dialog") {
      app.state.guides.runtimeState = stateBeforeSuspension || "running";
      stateBeforeSuspension = null;
      if (pendingAdvanceAfterSuspension) {
        pendingAdvanceAfterSuspension = false;
        nextGuideStep();
      }
    }
  }

  function activateGuideStep(guide, step, { reviewCompleted = false } = {}) {
    cleanupCompletionSubscription();
    app.state.guides.currentGuide = guide;
    app.state.guides.currentStep = step;
    app.state.guides.reviewingCompletedStep = reviewCompleted
      && app.state.guides.completedStepIds.has(step.id);
    if (step.reveal_id) {
      app.commands.revealGuideTarget(step.reveal_id, {
        reviewOnly: app.state.guides.reviewingCompletedStep,
      });
    }
    app.state.guides.runtimeState = !app.state.guides.reviewingCompletedStep
      && ["event", "interaction"].includes(step.completion.kind)
      ? "waiting_for_action"
      : "running";
    app.commands.renderGuideStep(guideRenderPayload(guide, step));
    if (app.state.guides.reviewingCompletedStep) return;
    if (step.completion.kind === "event") {
      completionDisposer = app.events.subscribe(step.completion.event, (detail) => {
        if (app.state.guides.currentStep !== step) return;
        app.commands.captureGuideCompletion(step, detail);
        if (!app.commands.guidePredicateSatisfied(step.completion.predicate_id)) return;
        app.state.guides.completedStepIds.add(step.id);
        cleanupCompletionSubscription();
        if (app.state.guides.runtimeState === "suspended_by_dialog") {
          pendingAdvanceAfterSuspension = true;
          return;
        }
        nextGuideStep();
      });
      if (
        step.completion.accept_preexisting
        && app.commands.guidePredicateSatisfied(step.completion.predicate_id)
      ) {
        app.state.guides.completedStepIds.add(step.id);
        cleanupCompletionSubscription();
        nextGuideStep();
      }
    } else if (step.completion.kind === "interaction" && controllerDocument) {
      const onInteraction = () => {
        if (app.state.guides.currentStep !== step) return;
        cleanupCompletionSubscription();
        endGuide({ restoreFocus: false });
      };
      controllerDocument.addEventListener("click", onInteraction, true);
      completionDisposer = () => {
        controllerDocument?.removeEventListener("click", onInteraction, true);
      };
    }
  }

  async function startGuide(guideId) {
    await runtimeCleanupPromise;
    const guide = app.commands.getGuideDefinition(guideId);
    if (!guide) return false;
    if (guideIsActive()) {
      const currentGuide = app.state.guides.currentGuide;
      const sameGuide = currentGuide?.id === guideId;
      const confirmed = await (app.commands.appConfirm?.(
        sameGuide
          ? "Restart this guide from its first step?"
          : "End the current guide and start this one?",
        sameGuide
          ? { title: "Restart Guide", ok: "Restart", cancel: "Keep Guide Open" }
          : { title: "Start Another Guide", ok: "End and Start", cancel: "Keep Guide Open" },
      ) ?? Promise.resolve(false));
      if (!confirmed) return false;
      endGuide({ restoreFocus: false });
      await runtimeCleanupPromise;
    }

    if (!await app.commands.prepareGuideRuntime(guide)) return false;
    closeGuidesCatalog({ restoreFocus: true });
    app.state.guides.completedStepIds = new Set();
    app.state.guides.completedDetourIds = new Set();
    app.state.guides.activeDetour = null;
    app.state.guides.reviewingCompletedStep = false;
    app.state.guides.presentationSnapshot = app.commands.captureGuidePresentation();
    activateGuideStep(guide, guide.steps[0]);
    return true;
  }

  function nextGuideStep() {
    const guide = app.state.guides.currentGuide;
    const step = app.state.guides.currentStep;
    if (!guide || !step || app.state.guides.runtimeState === "suspended_by_dialog") return false;
    if (
      !app.state.guides.reviewingCompletedStep
      && ["event", "interaction"].includes(step.completion.kind)
      && !app.state.guides.completedStepIds.has(step.id)
    ) {
      return false;
    }
    if (
      !app.state.guides.reviewingCompletedStep
      &&
      step.completion.kind === "manual"
      && step.completion.predicate_id
      && !app.commands.guidePredicateSatisfied(step.completion.predicate_id)
    ) {
      app.commands.showToast?.("Complete the requested guide action before continuing.", "warn");
      return false;
    }
    if (!app.state.guides.reviewingCompletedStep) {
      app.state.guides.completedStepIds.add(step.id);
    }
    const currentDetour = activeDetourDefinition(guide);
    const route = currentDetour?.steps || guide.steps;
    const index = route.indexOf(step);
    if (index >= route.length - 1) {
      if (!currentDetour) return completeGuide();
      if (
        currentDetour.return_predicate_id
        && !app.commands.guidePredicateSatisfied(currentDetour.return_predicate_id)
      ) {
        app.commands.showToast?.(
          "Restore the requested tutorial state before returning to the project.",
          "warn",
        );
        return false;
      }
      app.state.guides.completedDetourIds.add(currentDetour.id);
      app.state.guides.activeDetour = null;
      const returnStep = app.commands.getGuideStep(guide, currentDetour.return_step_id);
      activateGuideStep(guide, returnStep, { reviewCompleted: false });
      return true;
    }
    const nextStep = route[index + 1];
    activateGuideStep(guide, nextStep, {
      reviewCompleted: app.state.guides.completedStepIds.has(nextStep.id),
    });
    return true;
  }

  function previousGuideStep() {
    const guide = app.state.guides.currentGuide;
    const step = app.state.guides.currentStep;
    if (!guide || !step) return false;
    if (step.allow_previous === false) return false;
    const currentDetour = activeDetourDefinition(guide);
    const route = currentDetour?.steps || guide.steps;
    const index = route.indexOf(step);
    if (index <= 0) return false;
    const previousStep = route[index - 1];
    activateGuideStep(guide, previousStep, {
      reviewCompleted: app.state.guides.completedStepIds.has(previousStep.id),
    });
    return true;
  }

  function exitGuideDetour() {
    const guide = app.state.guides.currentGuide;
    const currentDetour = activeDetourDefinition(guide);
    if (!guide || !currentDetour) return false;
    cleanupCompletionSubscription();
    app.state.guides.activeDetour = null;
    const offerStep = app.commands.getGuideStep(guide, currentDetour.offer_step_id);
    activateGuideStep(guide, offerStep, {
      reviewCompleted: app.state.guides.completedStepIds.has(offerStep.id),
    });
    return true;
  }

  function exitCurrentGuideContext() {
    if (activeDetourDefinition()) return exitGuideDetour();
    endGuide();
    return true;
  }

  function endGuide({ restoreFocus = true } = {}) {
    cleanupCompletionSubscription();
    try {
      runtimeCleanupPromise = Promise.resolve(
        app.commands.cleanupGuideRuntime?.(app.state.guides.currentGuide),
      ).catch((error) => {
        console.error("[guides] runtime cleanup failed:", error);
        app.commands.showToast?.("The guide could not restore the previous settings state.", "warn");
      });
    } catch (error) {
      runtimeCleanupPromise = Promise.resolve();
      console.error("[guides] runtime cleanup failed:", error);
      app.commands.showToast?.("The guide could not restore the previous settings state.", "warn");
    }
    restorePrototypePresentation();
    app.commands.hideGuideOverlay({ restoreFocus });
    app.state.guides.runtimeState = "idle";
    app.state.guides.currentGuide = null;
    app.state.guides.currentStep = null;
    app.state.guides.completedStepIds = new Set();
    app.state.guides.completedDetourIds = new Set();
    app.state.guides.activeDetour = null;
    app.state.guides.reviewingCompletedStep = false;
    app.state.guides.runtimeContext = null;
    app.state.guides.stateBeforeTargetFailure = null;
    stateBeforeSuspension = null;
    pendingAdvanceAfterSuspension = false;
  }

  function completeGuide() {
    const guide = app.state.guides.currentGuide;
    if (!guide) return false;
    app.state.guides.runtimeState = "completed";
    endGuide();
    app.commands.showToast?.(`${guide.title} complete`, "success");
    return true;
  }

  function handleGuideTargetUnavailable(targetId) {
    if (app.state.guides.currentStep?.target_id !== targetId) return;
    if (app.state.guides.reviewingCompletedStep) return;
    if (app.state.guides.runtimeState === "target_unavailable") return;
    app.state.guides.stateBeforeTargetFailure = app.state.guides.runtimeState;
    app.state.guides.runtimeState = "target_unavailable";
    app.commands.showGuideTargetUnavailable();
  }

  function handleGuideTargetAvailable() {
    if (app.state.guides.runtimeState !== "target_unavailable") return;
    app.state.guides.runtimeState = app.state.guides.stateBeforeTargetFailure || "running";
    app.state.guides.stateBeforeTargetFailure = null;
    const guide = app.state.guides.currentGuide;
    const step = app.state.guides.currentStep;
    if (guide && step) {
      app.commands.renderGuideStep(guideRenderPayload(guide, step));
    }
  }

  function retryGuideTarget() {
    const guide = app.state.guides.currentGuide;
    const step = app.state.guides.currentStep;
    if (!guide || !step) return;
    app.state.guides.runtimeState = app.state.guides.stateBeforeTargetFailure || "running";
    app.commands.revealGuideTarget(step.reveal_id, {
      reviewOnly: app.state.guides.reviewingCompletedStep,
    });
    app.commands.renderGuideStep(guideRenderPayload(guide, step));
  }

  function startGuideDetour(detourId) {
    const guide = app.state.guides.currentGuide;
    const step = app.state.guides.currentStep;
    const currentDetour = app.commands.getGuideDetour(guide, detourId);
    if (
      !guide
      || !step
      || app.state.guides.activeDetour
      || !currentDetour
      || currentDetour.offer_step_id !== step.id
    ) return false;
    cleanupCompletionSubscription();
    app.state.guides.completedStepIds.add(step.id);
    app.state.guides.activeDetour = {
      id: currentDetour.id,
      offerStepId: currentDetour.offer_step_id,
      returnStepId: currentDetour.return_step_id,
    };
    activateGuideStep(guide, currentDetour.steps[0], {
      reviewCompleted: app.state.guides.completedStepIds.has(currentDetour.steps[0].id),
    });
    return true;
  }

  function moveGuideCard() {
    const current = Number.isInteger(app.state.guides.dockIndex)
      ? app.state.guides.dockIndex
      : -1;
    app.state.guides.dockIndex = (current + 1) % 4;
    app.commands.scheduleGuideOverlayLayout();
  }

  function handleHelpMenuAction(item) {
    const action = item.dataset.helpGuidesAction;
    if (action === "guided-setup") void startGuide("guided-setup");
    if (action === "generator-basics") void startGuide("prisma-generator-basics");
    if (action === "catalog") void openGuidesCatalog();
    if (action === "interface-preview") void startGuide("interface-preview");
  }

  function startGuideFollowup() {
    const followup = app.state.guides.currentStep?.followup;
    if (!followup || !app.commands.getGuideDefinition(followup.guide_id)) return false;
    endGuide({ restoreFocus: true });
    void startGuide(followup.guide_id);
    return true;
  }

  function trapCatalogFocus(event) {
    const modal = app.state.ui.$("#guidesCatalogModal");
    if (modal?.getAttribute("aria-hidden") !== "false") return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeGuidesCatalog();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...modal.querySelectorAll("button:not([disabled])")].filter(button => !button.hidden);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && controllerDocument?.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && controllerDocument?.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function initializeGuidesController({
    viewport = window,
    documentEvents = document,
    forceGuidedSetup = null,
  } = {}) {
    if (initialized) return;
    initialized = true;
    controllerDocument = documentEvents;
    if (typeof forceGuidedSetup === "boolean") {
      app.state.guides.forceGuidedSetup = forceGuidedSetup;
    } else {
      const search = viewport?.location?.search || "";
      app.state.guides.forceGuidedSetup = new URLSearchParams(search)
        .get("force-guided-setup") === "1";
    }
    app.commands.validateGuideDefinitions();
    app.commands.validateGuideTargetRegistry();
    app.commands.initializeGuideOverlay({ viewport, documentRoot: documentEvents });
    const button = app.state.ui.$("#helpGuidesMenuBtn");
    const menu = app.state.ui.$("#helpGuidesMenu");
    menuController = createAnchoredMenuController({
      button,
      menu,
      onActivate: handleHelpMenuAction,
      itemSelector: "[data-help-guides-action]",
      documentTarget: documentEvents,
      viewportTarget: viewport,
    });
    app.lifecycle.own(() => menuController?.destroy());
    app.lifecycle.own(() => firstLaunchWaitDisposer?.());

    const bindings = [
      ["#guidesCatalogClose", "click", () => closeGuidesCatalog()],
      ["#guidesCatalogList", "click", handleCatalogAction],
      ["#guideStepPrevious", "click", previousGuideStep],
      ["#guideStepNext", "click", nextGuideStep],
      ["#guideStepEnd", "click", exitCurrentGuideContext],
      ["#guideStepMove", "click", moveGuideCard],
      ["#guideStepRetry", "click", retryGuideTarget],
      ["#guideStepSkip", "click", nextGuideStep],
      ["#guideStepFollowupButton", "click", startGuideFollowup],
      ["#guideStepDetourButton", "click", () => {
        const detourId = app.state.ui.$("#guideStepDetourButton")?.dataset.detourId;
        if (detourId) startGuideDetour(detourId);
      }],
    ];
    for (const [selector, type, listener] of bindings) {
      const element = app.state.ui.$(selector);
      if (element) app.lifecycle.listen(element, type, listener);
    }
    const catalog = app.state.ui.$("#guidesCatalogModal");
    if (catalog) {
      app.lifecycle.listen(catalog, "click", event => {
        if (event.target === catalog) closeGuidesCatalog();
      });
      app.lifecycle.listen(catalog, "keydown", trapCatalogFocus);
    }
    app.lifecycle.listen(documentEvents, "keydown", event => {
      if (event.key !== "Escape" || !guideIsActive()) return;
      if (app.state.ui.$("#appDialog")?.getAttribute("aria-hidden") === "false") return;
      event.preventDefault();
      event.stopImmediatePropagation?.();
      exitCurrentGuideContext();
    });
  }

  Object.assign(app.commands, {
    closeGuidesCatalog,
    completeGuide,
    endGuide,
    exitGuideDetour,
    handleGuideTargetAvailable,
    handleGuideTargetUnavailable,
    getGuideProgressModel,
    getOfferedGuideDetour,
    initializeGuidesController,
    loadGuideState,
    maybeOfferGuidedSetup,
    moveGuideCard,
    nextGuideStep,
    openGuidesCatalog,
    persistWelcomeStatus,
    previousGuideStep,
    renderGuidesCatalog,
    retryGuideTarget,
    setGuideSuspended,
    startGuideDetour,
    startGuideFollowup,
    startGuide,
  });
  app.state.guides.FIRST_RUN_GUIDE_OFFER_ENABLED = FIRST_RUN_GUIDE_OFFER_ENABLED;
}
