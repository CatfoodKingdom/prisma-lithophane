import {
  alignRectToCssPixels,
  chooseGuideCardPlacement,
  intersectRects,
  normalizeRect,
  shadeRectsAroundTargets,
} from "../../core/guide-geometry.js?v=2026-08-01-guide-placement-continuity-v1";

const TARGET_WAIT_MS = 1400;

/**
 * Render the guide's deliberately small rich-text vocabulary. Guide copy may
 * mark strong emphasis with paired ** delimiters; every run is still assigned
 * through textContent, so definitions cannot inject arbitrary markup.
 */
export function renderGuideTextContent(element, text, documentRoot = element?.ownerDocument) {
  if (!element) return;
  const source = String(text ?? "");
  const pattern = /\*\*([^*\n]+)\*\*/g;
  const runs = [];
  let cursor = 0;
  let match = pattern.exec(source);
  while (match) {
    if (match.index > cursor) runs.push({ text: source.slice(cursor, match.index), strong: false });
    runs.push({ text: match[1], strong: true });
    cursor = pattern.lastIndex;
    match = pattern.exec(source);
  }
  if (cursor < source.length) runs.push({ text: source.slice(cursor), strong: false });
  if (!runs.length) runs.push({ text: source, strong: false });

  if (!documentRoot?.createElement || !documentRoot?.createTextNode || !element.replaceChildren) {
    element.textContent = runs.map(run => run.text).join("");
    return;
  }
  const nodes = runs.map((run) => {
    if (!run.strong) return documentRoot.createTextNode(run.text);
    const strong = documentRoot.createElement("strong");
    strong.textContent = run.text;
    return strong;
  });
  element.replaceChildren(...nodes);
}

/**
 * Bring a guide target into the vertical reading area without horizontally
 * recentering scroll containers such as the left sidebar.
 */
export function scrollGuideTargetIntoView(target, { block = "center" } = {}) {
  target?.scrollIntoView?.({
    block,
    inline: "nearest",
    behavior: "auto",
  });
}

/**
 * Reveal both ends of a semantic target region. Grouped regions often contain
 * several adjacent labels and controls; scrolling only the first element can
 * leave the final controls clipped at the bottom of a short viewport.
 */
export function scrollGuideRegionIntoView(region, { block = "nearest" } = {}) {
  const elements = Array.isArray(region) ? region.filter(Boolean) : [];
  if (!elements.length) return;
  scrollGuideTargetIntoView(elements[0], { block });
  if (elements.at(-1) !== elements[0]) {
    scrollGuideTargetIntoView(elements.at(-1), { block: "nearest" });
  }
}

/** Center a standalone guide card within the current visual viewport. */
export function centerStandaloneGuideCard({
  cardSize,
  viewportRect: rawViewport,
  margin = 8,
}) {
  const viewport = normalizeRect(rawViewport);
  const width = Math.min(
    Math.max(1, Number(cardSize?.width) || 1),
    Math.max(1, viewport.width - (2 * margin)),
  );
  const height = Math.min(
    Math.max(1, Number(cardSize?.height) || 1),
    Math.max(1, viewport.height - (2 * margin)),
  );
  return {
    left: viewport.left + ((viewport.width - width) / 2),
    top: viewport.top + ((viewport.height - height) / 2),
    placement: "center",
    docked: false,
  };
}

/** Report whether transient application UI should temporarily hide a guide. */
export function guideBlockingDialogOpen(app) {
  const dialog = app?.state?.ui?.$(".modal-overlay[aria-hidden=\"false\"]");
  if (
    dialog?.classList?.contains("modal-overlay")
    && dialog?.getAttribute?.("aria-hidden") === "false"
  ) return true;
  const openMenu = app?.state?.ui?.$(".topbar-menu:not([hidden])");
  if (openMenu && openMenu.hidden !== true) return true;
  const progress = app?.state?.ui?.$("#opProgress:not(.is-hidden)");
  return Boolean(progress && !progress.classList?.contains("is-hidden"));
}

/** Previous always stays within the active guide or detour route. */
export function guidePreviousDisabled({ stepIndex, allowPrevious }) {
  return allowPrevious === false || stepIndex === 0;
}

/** A route has nothing to navigate back to from its first step. */
export function guidePreviousHidden({ stepIndex, unavailable }) {
  return unavailable || stepIndex === 0;
}

/** Show only the overlay elements used by the current spotlight layout. */
export function syncGuideOverlayElementVisibility(elements, activeCount) {
  elements.forEach((element, index) => {
    element.classList.toggle("is-hidden", index >= activeCount);
  });
}

/** Install the guide-owned overlay renderer and responsive positioning loop. */
export function installFeaturesGuidesOverlay(app) {
  let initialized = false;
  let viewportTarget = null;
  let documentTarget = null;
  let root = null;
  let card = null;
  let frames = [];
  let shades = [];
  let currentStep = null;
  let currentGuide = null;
  let currentStepIndex = 0;
  let currentStepCount = 0;
  let currentRouteKind = "spine";
  let layoutScheduled = false;
  let scrollOnNextLayout = false;
  let targetDeadline = 0;
  let retryTimer = null;
  let resizeObserver = null;
  let mutationObserver = null;
  let observedTargets = [];
  let lastCardPosition = null;

  const now = () => globalThis.performance?.now?.() ?? Date.now();

  function viewportRect() {
    const visual = viewportTarget?.visualViewport;
    const left = visual?.offsetLeft || 0;
    const top = visual?.offsetTop || 0;
    const width = visual?.width || viewportTarget?.innerWidth || documentTarget.documentElement.clientWidth;
    const height = visual?.height || viewportTarget?.innerHeight || documentTarget.documentElement.clientHeight;
    return { left, top, right: left + width, bottom: top + height, width, height };
  }

  function clippedTargetRect(target) {
    let visible = intersectRects(target.getBoundingClientRect(), viewportRect());
    if (!visible) return null;
    let ancestor = target.parentElement;
    while (ancestor && ancestor !== documentTarget.body) {
      const style = viewportTarget.getComputedStyle(ancestor);
      const clipsX = /(auto|scroll|hidden|clip)/.test(style.overflowX || style.overflow);
      const clipsY = /(auto|scroll|hidden|clip)/.test(style.overflowY || style.overflow);
      if (clipsX || clipsY) {
        const bounds = ancestor.getBoundingClientRect();
        const clipRect = {
          left: clipsX ? bounds.left : visible.left,
          right: clipsX ? bounds.right : visible.right,
          top: clipsY ? bounds.top : visible.top,
          bottom: clipsY ? bounds.bottom : visible.bottom,
        };
        visible = intersectRects(visible, clipRect);
        if (!visible) return null;
      }
      ancestor = ancestor.parentElement;
    }
    return visible;
  }

  function applyRect(element, rect) {
    if (!element) return;
    const aligned = alignRectToCssPixels(rect);
    element.style.left = `${aligned.left}px`;
    element.style.top = `${aligned.top}px`;
    element.style.width = `${aligned.width}px`;
    element.style.height = `${aligned.height}px`;
  }

  function ensureOverlayElements(elements, count, className) {
    while (elements.length < count) {
      const element = documentTarget.createElement("div");
      element.className = className;
      element.setAttribute("aria-hidden", "true");
      root.insertBefore(element, card);
      elements.push(element);
    }
    syncGuideOverlayElementVisibility(elements, count);
  }

  function setFrameVisible(visible) {
    frames.forEach(frame => frame.classList.toggle("is-hidden", !visible));
    shades.forEach(shade => shade.classList.toggle("is-hidden", !visible));
  }

  function observeTargets(targets) {
    if (
      !resizeObserver
      || (
        observedTargets.length === targets.length
        && observedTargets.every((target, index) => target === targets[index])
      )
    ) {
      return;
    }
    resizeObserver.disconnect();
    resizeObserver.observe(card);
    targets.forEach(target => resizeObserver.observe(target));
    observedTargets = [...targets];
  }

  function unionRects(rects) {
    const normalized = rects.map(normalizeRect);
    const left = Math.min(...normalized.map(rect => rect.left));
    const top = Math.min(...normalized.map(rect => rect.top));
    const right = Math.max(...normalized.map(rect => rect.right));
    const bottom = Math.max(...normalized.map(rect => rect.bottom));
    return normalizeRect({ left, top, right, bottom });
  }

  function clippedRegionRect(elements) {
    const visibleRects = elements.map(clippedTargetRect).filter(Boolean);
    return visibleRects.length > 0 ? unionRects(visibleRects) : null;
  }

  function positionDockedCard() {
    const viewport = viewportRect();
    const placement = chooseGuideCardPlacement({
      targetRect: viewport,
      cardSize: { width: card.offsetWidth, height: card.offsetHeight },
      viewportRect: viewport,
      dockIndex: Number.isInteger(app.state.guides.dockIndex) ? app.state.guides.dockIndex : 0,
    });
    card.style.left = `${Math.round(placement.left)}px`;
    card.style.top = `${Math.round(placement.top)}px`;
    card.dataset.placement = placement.placement;
  }

  function positionStandaloneCard() {
    if (Number.isInteger(app.state.guides.dockIndex)) {
      positionDockedCard();
      return;
    }
    const placement = centerStandaloneGuideCard({
      cardSize: { width: card.offsetWidth, height: card.offsetHeight },
      viewportRect: viewportRect(),
    });
    card.style.left = `${Math.round(placement.left)}px`;
    card.style.top = `${Math.round(placement.top)}px`;
    card.dataset.placement = placement.placement;
  }

  function performLayout() {
    layoutScheduled = false;
    if (!root || root.classList.contains("is-hidden") || !currentStep) return;
    if (guideBlockingDialogOpen(app)) {
      root.classList.add("is-suspended");
      app.commands.setGuideSuspended?.(true);
      return;
    }
    root.classList.remove("is-suspended");
    app.commands.setGuideSuspended?.(false);
    const viewport = viewportRect();
    card.style.maxWidth = `${Math.max(1, viewport.width - 16)}px`;
    card.style.maxHeight = `${Math.max(1, viewport.height - 16)}px`;

    if (!currentStep.target_id) {
      setFrameVisible(false);
      observeTargets([]);
      positionStandaloneCard();
      return;
    }

    const targetRegions = app.commands.resolveGuideTargetRegions(currentStep.target_id);
    if (!targetRegions) {
      setFrameVisible(false);
      observeTargets([]);
      positionDockedCard();
      if (app.state.guides.reviewingCompletedStep) return;
      app.state.ui.$("#guideStepStatus").textContent = "Locating this control…";
      if (now() >= targetDeadline) {
        app.commands.handleGuideTargetUnavailable?.(currentStep.target_id);
      } else if (retryTimer === null) {
        retryTimer = viewportTarget.setTimeout(() => {
          retryTimer = null;
          scheduleGuideOverlayLayout();
        }, 100);
      }
      return;
    }

    if (scrollOnNextLayout) {
      scrollOnNextLayout = false;
      if (targetRegions.length === 1) {
        scrollGuideRegionIntoView(targetRegions[0], { block: "center" });
      } else {
        targetRegions.forEach(region => scrollGuideRegionIntoView(region));
      }
      scheduleGuideOverlayLayout();
      return;
    }

    const visibleTargets = targetRegions.map(clippedRegionRect);
    if (visibleTargets.some(target => !target)) {
      if (app.state.guides.reviewingCompletedStep) {
        setFrameVisible(false);
        observeTargets([]);
        positionDockedCard();
        return;
      }
      targetRegions.forEach(region => scrollGuideRegionIntoView(region));
      if (now() >= targetDeadline) {
        app.commands.handleGuideTargetUnavailable?.(currentStep.target_id);
      } else {
        scheduleGuideOverlayLayout();
      }
      return;
    }

    app.commands.handleGuideTargetAvailable?.();
    observeTargets(targetRegions.flat());
    const padding = 4;
    const framedTargets = visibleTargets.map(visibleTarget => (
      intersectRects(
        {
          left: visibleTarget.left - padding,
          top: visibleTarget.top - padding,
          right: visibleTarget.right + padding,
          bottom: visibleTarget.bottom + padding,
        },
        viewport,
      ) || visibleTarget
    ));
    ensureOverlayElements(frames, framedTargets.length, "guide-target-frame");
    framedTargets.forEach((rect, index) => applyRect(frames[index], rect));
    const shadeLayout = shadeRectsAroundTargets(framedTargets, viewport);
    ensureOverlayElements(shades, shadeLayout.length, "guide-overlay-shade");
    shadeLayout.forEach((rect, index) => applyRect(shades[index], rect));

    const placementTarget = unionRects(framedTargets);
    const placement = chooseGuideCardPlacement({
      targetRect: placementTarget,
      cardSize: { width: card.offsetWidth, height: card.offsetHeight },
      viewportRect: viewport,
      preferred: currentStep.preferred_placements,
      dockIndex: app.state.guides.dockIndex,
      previousPosition: currentStep.placement_group ? lastCardPosition : null,
      avoidRects: framedTargets,
    });
    card.style.left = `${Math.round(placement.left)}px`;
    card.style.top = `${Math.round(placement.top)}px`;
    card.dataset.placement = placement.placement;
    lastCardPosition = { left: placement.left, top: placement.top };
  }

  function scheduleGuideOverlayLayout({ scrollTarget = false } = {}) {
    if (!root || root.classList.contains("is-hidden")) return;
    scrollOnNextLayout ||= scrollTarget;
    if (layoutScheduled) return;
    layoutScheduled = true;
    viewportTarget.requestAnimationFrame(performLayout);
  }

  function syncStepControls({ unavailable = false } = {}) {
    const actions = app.state.ui.$(".guide-step-actions");
    const end = app.state.ui.$("#guideStepEnd");
    const previous = app.state.ui.$("#guideStepPrevious");
    const next = app.state.ui.$("#guideStepNext");
    const retry = app.state.ui.$("#guideStepRetry");
    const skip = app.state.ui.$("#guideStepSkip");
    const status = app.state.ui.$("#guideStepStatus");
    const isLast = currentStepIndex === currentStepCount - 1;
    const dismissOnInteraction = currentStep.completion.kind === "interaction";
    if (actions) actions.hidden = dismissOnInteraction;
    if (end) {
      const inDetour = currentRouteKind === "detour";
      end.textContent = inDetour ? "Exit Detour" : "End Guide";
      end.setAttribute(
        "aria-label",
        inDetour ? "Exit detour and return to the parent guide" : "End guide",
      );
    }
    if (previous) {
      previous.disabled = guidePreviousDisabled({
        stepIndex: currentStepIndex,
        routeKind: currentRouteKind,
        allowPrevious: currentStep.allow_previous,
      });
      previous.hidden = guidePreviousHidden({
        stepIndex: currentStepIndex,
        unavailable,
      });
    }
    if (next) {
      next.hidden = unavailable
        || (
          currentStep.completion.kind === "event"
          && !app.state.guides.reviewingCompletedStep
        );
      next.textContent = currentStep.next_label
        || (isLast ? (currentRouteKind === "detour" ? "Return" : "Finish") : "Next");
    }
    if (retry) retry.hidden = !unavailable;
    if (skip) skip.hidden = !unavailable || !currentStep.allow_skip;
    if (status) {
      status.textContent = unavailable
        ? "This control is not currently available."
        : dismissOnInteraction
          ? "Select any control to dismiss this message."
        : app.state.guides.reviewingCompletedStep
          ? "This step is already complete."
        : currentStep.completion.kind === "event"
          ? "Waiting for this action…"
          : "";
    }
  }

  function syncStepFollowup() {
    const container = app.state.ui.$("#guideStepFollowup");
    const text = app.state.ui.$("#guideStepFollowupText");
    const button = app.state.ui.$("#guideStepFollowupButton");
    const followup = currentStep?.followup;
    const available = !!followup && !!app.commands.getGuideDefinition(followup.guide_id);
    if (container) container.hidden = !available;
    if (!available) return;
    if (text) text.textContent = followup.text;
    if (button) button.textContent = followup.label;
  }

  function syncStepDetour() {
    const container = app.state.ui.$("#guideStepDetour");
    const text = app.state.ui.$("#guideStepDetourText");
    const button = app.state.ui.$("#guideStepDetourButton");
    const currentDetour = app.commands.getOfferedGuideDetour?.();
    if (container) container.hidden = !currentDetour;
    if (!currentDetour) {
      if (button) delete button.dataset.detourId;
      return;
    }
    if (text) text.textContent = currentDetour.description;
    if (button) {
      button.textContent = `Explore ${currentDetour.label}`;
      button.dataset.detourId = currentDetour.id;
    }
  }

  function syncGuideProgress() {
    const rootElement = app.state.ui.$("#guideProgress");
    const label = app.state.ui.$("#guideProgressLabel");
    const count = app.state.ui.$("#guideProgressCount");
    const track = app.state.ui.$("#guideProgressTrack");
    const model = app.commands.getGuideProgressModel?.();
    if (!rootElement || !track || !model) return;
    rootElement.dataset.mode = model.mode;
    if (label) label.textContent = model.label;
    if (count) count.textContent = model.count;
    track.setAttribute("aria-valuemin", "1");
    track.setAttribute("aria-valuemax", String(model.max));
    track.setAttribute("aria-valuenow", String(model.value));
    track.setAttribute("aria-valuetext", model.valueText);
    const documentRoot = track.ownerDocument || documentTarget;
    if (!documentRoot?.createElement || !track.replaceChildren) return;
    const segments = model.segments.map((segment) => {
      const element = documentRoot.createElement("span");
      element.className = `guide-progress__segment is-${segment.state}`;
      element.style.setProperty(
        "--guide-progress-width",
        `${Math.max(0, Math.min(1, Number(segment.fraction) || 0)) * 100}%`,
      );
      element.title = segment.label;
      element.setAttribute("aria-hidden", "true");
      return element;
    });
    track.replaceChildren(...segments);
  }

  function renderGuideStep({
    guide,
    step,
    stepIndex,
    stepCount = guide.steps.length,
    routeKind = "spine",
  }) {
    const retainsPlacement = (
      currentGuide?.id === guide.id
      && currentStep?.placement_group
      && currentStep.placement_group === step.placement_group
    );
    currentGuide = guide;
    currentStep = step;
    currentStepIndex = stepIndex;
    currentStepCount = stepCount;
    currentRouteKind = routeKind;
    targetDeadline = now() + TARGET_WAIT_MS;
    if (!retainsPlacement) {
      lastCardPosition = null;
    }
    app.state.guides.dockIndex = null;
    app.state.ui.$("#guideStepTitle").textContent = step.title;
    const body = app.state.ui.$("#guideStepBody");
    renderGuideTextContent(
      body,
      app.commands.formatGuideText(step.body),
      body?.ownerDocument || documentTarget,
    );
    syncStepFollowup();
    syncStepDetour();
    syncGuideProgress();
    root.classList.remove("is-hidden", "is-suspended");
    root.setAttribute("aria-hidden", "false");
    syncStepControls();
    mutationObserver?.observe(documentTarget.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["aria-hidden", "class", "hidden"],
    });
    scheduleGuideOverlayLayout({ scrollTarget: true });
  }

  function showGuideTargetUnavailable() {
    if (!currentStep) return;
    setFrameVisible(false);
    syncStepControls({ unavailable: true });
    positionDockedCard();
  }

  function hideGuideOverlay({ restoreFocus = true } = {}) {
    if (!root) return;
    const focusWasInside = root.contains?.(documentTarget.activeElement);
    root.classList.add("is-hidden");
    root.classList.remove("is-suspended");
    root.setAttribute("aria-hidden", "true");
    currentStep = null;
    currentGuide = null;
    lastCardPosition = null;
    app.state.guides.dockIndex = null;
    mutationObserver?.disconnect();
    resizeObserver?.disconnect();
    observedTargets = [];
    if (retryTimer !== null) {
      viewportTarget.clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (restoreFocus && focusWasInside) {
      app.state.ui.$("#helpGuidesMenuBtn")?.focus?.();
    }
  }

  function initializeGuideOverlay({
    viewport = window,
    documentRoot = document,
  } = {}) {
    if (initialized) return;
    initialized = true;
    viewportTarget = viewport;
    documentTarget = documentRoot;
    root = app.state.ui.$("#guideOverlayRoot");
    card = app.state.ui.$("#guideStepCard");
    frames = [...app.state.ui.$$(".guide-target-frame")];
    shades = [...app.state.ui.$$(".guide-overlay-shade")];
    resizeObserver = typeof ResizeObserver === "function"
      ? new ResizeObserver(() => scheduleGuideOverlayLayout())
      : null;
    mutationObserver = typeof MutationObserver === "function"
      ? new MutationObserver(() => scheduleGuideOverlayLayout())
      : null;
    app.lifecycle.listen(viewportTarget, "resize", () => scheduleGuideOverlayLayout());
    app.lifecycle.listen(documentRoot, "scroll", () => scheduleGuideOverlayLayout(), true);
    if (viewportTarget.visualViewport) {
      app.lifecycle.listen(viewportTarget.visualViewport, "resize", () => scheduleGuideOverlayLayout());
      app.lifecycle.listen(viewportTarget.visualViewport, "scroll", () => scheduleGuideOverlayLayout());
    }
    app.lifecycle.own(() => {
      mutationObserver?.disconnect();
      resizeObserver?.disconnect();
    });
  }

  Object.assign(app.commands, {
    hideGuideOverlay,
    initializeGuideOverlay,
    renderGuideStep,
    scheduleGuideOverlayLayout,
    showGuideTargetUnavailable,
  });
}
