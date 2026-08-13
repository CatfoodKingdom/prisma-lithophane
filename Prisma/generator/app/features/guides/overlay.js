import {
  alignRectToCssPixels,
  chooseGuideCardPlacement,
  chooseGuideSurfaceLayout,
  intersectRects,
  normalizeRect,
  shadeRectsAroundTargets,
} from "../../core/guide-geometry.js?v=2026-08-12-guide-companion-v4";

const TARGET_WAIT_MS = 1400;

/**
 * Render the guide's deliberately small rich-text vocabulary. Guide copy may
 * mark strong emphasis with paired ** delimiters and regular emphasis with
 * paired * delimiters. Every run is still assigned through textContent, so
 * definitions cannot inject arbitrary markup.
 */
export function renderGuideTextContent(element, text, documentRoot = element?.ownerDocument) {
  if (!element) return;
  const source = String(text ?? "");
  const pattern = /\*\*([^*\n]+)\*\*|\*([^*\n]+)\*/g;
  const runs = [];
  let cursor = 0;
  let match = pattern.exec(source);
  while (match) {
    if (match.index > cursor) runs.push({ text: source.slice(cursor, match.index), emphasis: null });
    runs.push({
      text: match[1] || match[2],
      emphasis: match[1] ? "strong" : "regular",
    });
    cursor = pattern.lastIndex;
    match = pattern.exec(source);
  }
  if (cursor < source.length) runs.push({ text: source.slice(cursor), emphasis: null });
  if (!runs.length) runs.push({ text: source, emphasis: null });

  if (!documentRoot?.createElement || !documentRoot?.createTextNode || !element.replaceChildren) {
    element.textContent = runs.map(run => run.text).join("");
    return;
  }
  const nodes = runs.map((run) => {
    if (!run.emphasis) return documentRoot.createTextNode(run.text);
    const emphasis = documentRoot.createElement(run.emphasis === "strong" ? "strong" : "em");
    emphasis.textContent = run.text;
    return emphasis;
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
  const participating = new Set(app?.state?.guides?.currentStep?.participating_surfaces || []);
  const firstDialog = app?.state?.ui?.$(".modal-overlay[aria-hidden=\"false\"]");
  const dialogs = [...(app?.state?.ui?.$$?.(".modal-overlay[aria-hidden=\"false\"]") || (firstDialog ? [firstDialog] : []))];
  if (dialogs.some(dialog => dialog?.classList?.contains("modal-overlay")
    && dialog?.getAttribute?.("aria-hidden") === "false"
    && (!dialog.id || !participating.has(`#${dialog.id}`)))) return true;
  const firstMenu = app?.state?.ui?.$(".topbar-menu:not([hidden])");
  const menus = [...(app?.state?.ui?.$$?.(".topbar-menu:not([hidden])") || (firstMenu ? [firstMenu] : []))];
  if (menus.some(menu => menu.hidden !== true && (!menu.id || !participating.has(`#${menu.id}`)))) return true;
  const progress = app?.state?.ui?.$("#opProgress:not(.is-hidden)");
  return Boolean(progress && !progress.classList?.contains("is-hidden") && !participating.has("#opProgress"));
}

/** Place a Guide card at a stable viewport reading position through modal phases. */
export function anchorGuideCardInViewport({
  anchor,
  cardSize,
  viewportRect: rawViewport,
  avoidRects = [],
  gap = 10,
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
  const horizontalFraction = anchor === "center-left"
    ? 0.25
    : anchor === "center-right" ? 0.67 : 0.5;
  const left = Math.max(
    viewport.left + margin,
    Math.min(
      viewport.left + (viewport.width * horizontalFraction) - (width / 2),
      viewport.right - width - margin,
    ),
  );
  const top = Math.max(
    viewport.top + margin,
    Math.min(
      viewport.top + ((viewport.height - height) / 2),
      viewport.bottom - height - margin,
    ),
  );
  const cardRect = normalizeRect({ left, top, right: left + width, bottom: top + height });
  const obscuresTarget = avoidRects.some((rawRect) => {
    const rect = normalizeRect(rawRect);
    return Boolean(intersectRects(cardRect, {
      left: rect.left - gap,
      top: rect.top - gap,
      right: rect.right + gap,
      bottom: rect.bottom + gap,
    }));
  });
  if (obscuresTarget) return null;
  return { left, top, placement: `viewport-${anchor}`, docked: false };
}

/** Previous always stays within the active guide or detour route. */
export function guidePreviousDisabled({
  stepIndex,
  allowPrevious,
  previousReturnsToOffer = false,
  suppressPrevious = false,
}) {
  return allowPrevious === false
    || suppressPrevious
    || (stepIndex === 0 && !previousReturnsToOffer);
}

/** A route has nothing to navigate back to from its first step. */
export function guidePreviousHidden({
  stepIndex,
  unavailable,
  previousReturnsToOffer = false,
  suppressPrevious = false,
}) {
  return unavailable
    || suppressPrevious
    || (stepIndex === 0 && !previousReturnsToOffer);
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
  let companion = null;
  let companionContent = null;
  let mediaLightbox = null;
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
  let observedCompanion = false;
  let lastCardPosition = null;
  let lastCompanionPlacement = null;
  let companionRenderRevision = 0;
  let companionConstraints = null;
  let mediaLightboxTrigger = null;

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
    const shouldObserveCompanion = Boolean(companion && !companion.hidden);
    if (
      !resizeObserver
      || (
        observedCompanion === shouldObserveCompanion
        &&
        observedTargets.length === targets.length
        && observedTargets.every((target, index) => target === targets[index])
      )
    ) {
      return;
    }
    resizeObserver.disconnect();
    resizeObserver.observe(card);
    if (shouldObserveCompanion) resizeObserver.observe(companion);
    targets.forEach(target => resizeObserver.observe(target));
    observedTargets = [...targets];
    observedCompanion = shouldObserveCompanion;
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

  function applySurfacePosition(element, position) {
    if (!element || !position) return;
    element.style.left = `${Math.round(position.left)}px`;
    element.style.top = `${Math.round(position.top)}px`;
    element.dataset.placement = position.placement || "";
  }

  function clearCompanionConstraints() {
    companionConstraints = null;
    companion?.style?.removeProperty?.("--guide-companion-layout-max-width");
    companion?.style?.removeProperty?.("--guide-companion-layout-max-height");
  }

  function applyCompanionConstraints(constraints) {
    if (!companion || !constraints) return false;
    const normalized = {
      maxWidth: Math.max(1, Math.floor(Number(constraints.maxWidth) || 1)),
      maxHeight: Math.max(1, Math.floor(Number(constraints.maxHeight) || 1)),
    };
    if (
      companionConstraints?.maxWidth === normalized.maxWidth
      && companionConstraints?.maxHeight === normalized.maxHeight
    ) return false;
    companionConstraints = normalized;
    companion.style.setProperty("--guide-companion-layout-max-width", `${normalized.maxWidth}px`);
    companion.style.setProperty("--guide-companion-layout-max-height", `${normalized.maxHeight}px`);
    return true;
  }

  function closeGuideMediaLightbox({ restoreFocus = true } = {}) {
    if (!mediaLightbox || mediaLightbox.hidden) return false;
    mediaLightbox.hidden = true;
    mediaLightbox.setAttribute("aria-hidden", "true");
    app.state.ui.$("#guideMediaLightboxImage")?.removeAttribute?.("src");
    if (restoreFocus && mediaLightboxTrigger?.isConnected) mediaLightboxTrigger.focus?.();
    mediaLightboxTrigger = null;
    return true;
  }

  function openGuideMediaLightbox(itemIndex, trigger) {
    const item = currentStep?.companion?.items?.[Number(itemIndex)];
    if (!mediaLightbox || item?.type !== "image" || item.expandable === false) return false;
    const image = app.state.ui.$("#guideMediaLightboxImage");
    const title = app.state.ui.$("#guideMediaLightboxTitle");
    if (!image || !title) return false;
    image.src = item.src;
    image.alt = item.alt || "";
    title.textContent = item.lightbox_title || item.caption || currentStep.companion.title || "";
    mediaLightboxTrigger = trigger || null;
    mediaLightbox.hidden = false;
    mediaLightbox.setAttribute("aria-hidden", "false");
    app.state.ui.$("#guideMediaLightboxClose")?.focus?.();
    return true;
  }

  function syncStepCompanion() {
    companionRenderRevision += 1;
    const revision = companionRenderRevision;
    closeGuideMediaLightbox({ restoreFocus: false });
    clearCompanionConstraints();
    if (!companion || !companionContent) return;
    companionContent.replaceChildren?.();
    const model = currentStep?.companion;
    companion.hidden = !model;
    if (!model) return;
    const header = app.state.ui.$("#guideCompanionHeader");
    const title = app.state.ui.$("#guideCompanionTitle");
    const headerExpand = app.state.ui.$("#guideCompanionExpand");
    const expandableIndexes = model.items
      .map((item, itemIndex) => (item.type === "image" && item.expandable !== false ? itemIndex : null))
      .filter(itemIndex => itemIndex !== null);
    if (header) header.hidden = false;
    if (title) title.textContent = model.title || "";
    if (headerExpand) {
      const hasSingleExpandableImage = expandableIndexes.length === 1;
      headerExpand.hidden = !hasSingleExpandableImage;
      if (hasSingleExpandableImage) {
        headerExpand.dataset.guideCompanionItem = String(expandableIndexes[0]);
      } else {
        delete headerExpand.dataset.guideCompanionItem;
      }
    }
    companionContent.dataset.layout = model.layout;

    const nodes = model.items.map((item, itemIndex) => {
      if (item.type === "text") {
        const paragraph = documentTarget.createElement("p");
        paragraph.className = "guide-companion__item guide-companion__text";
        renderGuideTextContent(paragraph, item.text, documentTarget);
        return paragraph;
      }
      const figure = documentTarget.createElement("figure");
      figure.className = "guide-companion__item guide-companion__figure";
      const imageControl = documentTarget.createElement(item.expandable === false ? "div" : "button");
      imageControl.className = item.expandable === false
        ? "guide-companion__image-static"
        : "guide-companion__image-button";
      if (item.expandable !== false) {
        imageControl.type = "button";
        imageControl.dataset.guideCompanionItem = String(itemIndex);
        imageControl.title = "Enlarge image";
      }
      const image = documentTarget.createElement("img");
      image.className = "guide-companion__image";
      image.alt = item.alt || "";
      imageControl.append(image);
      figure.append(imageControl);
      if (item.caption) {
        const caption = documentTarget.createElement("figcaption");
        caption.className = "guide-companion__caption";
        caption.textContent = item.caption;
        figure.append(caption);
      }
      image.addEventListener("load", () => {
        if (revision !== companionRenderRevision || !image.isConnected) return;
        if (image.naturalWidth > 0) image.style.width = `${image.naturalWidth}px`;
        clearCompanionConstraints();
        scheduleGuideOverlayLayout();
      });
      image.addEventListener("error", () => {
        if (revision !== companionRenderRevision || !image.isConnected) return;
        imageControl.hidden = true;
        if (headerExpand?.dataset?.guideCompanionItem === String(itemIndex)) {
          headerExpand.hidden = true;
          delete headerExpand.dataset.guideCompanionItem;
        }
        const failure = documentTarget.createElement("p");
        failure.className = "guide-companion__failure";
        failure.textContent = "This guide image could not be loaded.";
        figure.append(failure);
        clearCompanionConstraints();
        scheduleGuideOverlayLayout();
      });
      image.src = item.src;
      return figure;
    });
    companionContent.replaceChildren(...nodes);
  }

  function positionPairedSurfaces({ targetRect = null, avoidRects = [] } = {}) {
    if (!companion || companion.hidden) return false;
    const placement = chooseGuideSurfaceLayout({
      targetRect,
      cardSize: { width: card.offsetWidth, height: card.offsetHeight },
      companionSize: { width: companion.offsetWidth, height: companion.offsetHeight },
      viewportRect: viewportRect(),
      preferred: currentStep.preferred_placements,
      companionPreferred: currentStep.companion.preferred_placements,
      dockIndex: app.state.guides.dockIndex,
      viewportAnchor: currentStep.viewport_anchor,
      previousPosition: currentStep.placement_group ? lastCardPosition : null,
      previousCompanionPlacement: currentStep.placement_group ? lastCompanionPlacement : null,
      avoidRects,
    });
    if (!placement) return false;
    if (placement.companionConstraints && applyCompanionConstraints(placement.companionConstraints)) {
      scheduleGuideOverlayLayout();
      return true;
    }
    applySurfacePosition(card, placement.card);
    applySurfacePosition(companion, placement.companion);
    lastCardPosition = { left: placement.card.left, top: placement.card.top };
    lastCompanionPlacement = {
      side: placement.companion.side,
      alignment: placement.companion.alignment,
    };
    return true;
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
      root.classList.remove("is-interrupt-elevated");
      root.classList.add("is-suspended");
      app.commands.setGuideSuspended?.(true);
      return;
    }
    root.classList.remove("is-suspended");
    root.classList.toggle(
      "is-interrupt-elevated",
      app.state.guides.currentStep?.participating_surfaces?.includes("#appDialog")
        && app.state.ui.$("#appDialog")?.getAttribute("aria-hidden") === "false",
    );
    app.commands.setGuideSuspended?.(false);
    const viewport = viewportRect();
    card.style.maxWidth = `${Math.max(1, viewport.width - 16)}px`;
    card.style.maxHeight = `${Math.max(1, viewport.height - 16)}px`;

    if (!currentStep.target_id) {
      if (currentStep.overlay_mode === "full-scrim") {
        ensureOverlayElements(frames, 0, "guide-target-frame");
        ensureOverlayElements(shades, 1, "guide-overlay-shade");
        applyRect(shades[0], viewport);
      } else {
        setFrameVisible(false);
      }
      observeTargets([]);
      if (positionPairedSurfaces()) return;
      positionStandaloneCard();
      return;
    }

    const targetLayout = app.commands.resolveGuideTargetLayout?.(currentStep.target_id)
      || (() => {
        const regions = app.commands.resolveGuideTargetRegions(currentStep.target_id);
        return regions
          ? { spotlightRegions: regions, frameRegions: regions, placementRegions: regions }
          : null;
      })();
    if (!targetLayout) {
      setFrameVisible(false);
      observeTargets([]);
      if (!positionPairedSurfaces()) positionDockedCard();
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
    const {
      spotlightRegions,
      frameRegions,
      placementRegions = spotlightRegions,
    } = targetLayout;
    const layoutRegions = [...spotlightRegions, ...frameRegions, ...placementRegions]
      .filter((region, index, regions) => regions.findIndex(candidate => (
        candidate.length === region.length
        && candidate.every((element, elementIndex) => element === region[elementIndex])
      )) === index);

    if (scrollOnNextLayout) {
      scrollOnNextLayout = false;
      if (layoutRegions.length === 1) {
        scrollGuideRegionIntoView(layoutRegions[0], { block: "center" });
      } else {
        layoutRegions.forEach(region => scrollGuideRegionIntoView(region));
      }
      scheduleGuideOverlayLayout();
      return;
    }

    const visibleSpotlights = spotlightRegions.map(clippedRegionRect);
    const visibleFrames = frameRegions.map(clippedRegionRect);
    const visiblePlacements = placementRegions.map(clippedRegionRect);
    if ([...visibleSpotlights, ...visibleFrames, ...visiblePlacements].some(target => !target)) {
      if (app.state.guides.reviewingCompletedStep) {
        setFrameVisible(false);
        observeTargets([]);
        if (!positionPairedSurfaces()) positionDockedCard();
        return;
      }
      layoutRegions.forEach(region => scrollGuideRegionIntoView(region));
      if (now() >= targetDeadline) {
        app.commands.handleGuideTargetUnavailable?.(currentStep.target_id);
      } else {
        scheduleGuideOverlayLayout();
      }
      return;
    }

    app.commands.handleGuideTargetAvailable?.();
    observeTargets([...new Set(layoutRegions.flat())]);
    const padding = 4;
    const padTarget = visibleTarget => (
      intersectRects(
        {
          left: visibleTarget.left - padding,
          top: visibleTarget.top - padding,
          right: visibleTarget.right + padding,
          bottom: visibleTarget.bottom + padding,
        },
        viewport,
      ) || visibleTarget
    );
    const spotlightTargets = visibleSpotlights.map(padTarget);
    const framedTargets = visibleFrames;
    const placementTargets = visiblePlacements.map(padTarget);
    ensureOverlayElements(frames, framedTargets.length, "guide-target-frame");
    framedTargets.forEach((rect, index) => applyRect(frames[index], rect));
    const shadeLayout = shadeRectsAroundTargets(spotlightTargets, viewport);
    ensureOverlayElements(shades, shadeLayout.length, "guide-overlay-shade");
    shadeLayout.forEach((rect, index) => applyRect(shades[index], rect));

    const placementTarget = unionRects(placementTargets);
    const avoidanceTargets = [...spotlightTargets, ...framedTargets].filter((rect, index, rects) => (
      rects.findIndex(candidate => (
        candidate.left === rect.left
        && candidate.top === rect.top
        && candidate.right === rect.right
        && candidate.bottom === rect.bottom
      )) === index
    ));
    if (positionPairedSurfaces({ targetRect: placementTarget, avoidRects: avoidanceTargets })) return;
    const cardSize = { width: card.offsetWidth, height: card.offsetHeight };
    const anchoredPlacement = currentStep.viewport_anchor && !Number.isInteger(app.state.guides.dockIndex)
      ? anchorGuideCardInViewport({
        anchor: currentStep.viewport_anchor,
        cardSize,
        viewportRect: viewport,
        avoidRects: spotlightTargets,
      })
      : null;
    const placement = anchoredPlacement || chooseGuideCardPlacement({
      targetRect: placementTarget,
      cardSize,
      viewportRect: viewport,
      preferred: currentStep.preferred_placements,
      dockIndex: app.state.guides.dockIndex,
      previousPosition: currentStep.placement_group ? lastCardPosition : null,
      avoidRects: spotlightTargets,
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
      const finalDetourStep = inDetour && isLast;
      const detour = inDetour
        ? app.commands.getGuideDetour?.(
          app.state.guides.currentGuide,
          app.state.guides.activeDetour?.id,
        )
        : null;
      const finalExitDisabled = finalDetourStep && !detour?.allow_exit_on_final;
      const stepExitDisabled = currentStep.allow_end === false;
      const exitDisabled = finalExitDisabled || stepExitDisabled;
      end.textContent = inDetour ? (detour?.exit_label || "Exit Detour") : "End Guide";
      end.disabled = exitDisabled;
      end.title = finalExitDisabled
        ? "Use Return to complete this detour"
        : stepExitDisabled ? "Use Finish to complete this guide" : "";
      end.setAttribute(
        "aria-label",
        finalExitDisabled
          ? "Exit detour unavailable on the final step; use Return to complete the detour"
          : stepExitDisabled
            ? "End guide unavailable on the final step; use Finish to complete the guide"
          : inDetour ? `${end.textContent} and return to the parent guide` : "End guide",
      );
    }
    if (previous) {
      previous.disabled = guidePreviousDisabled({
        stepIndex: currentStepIndex,
        allowPrevious: currentStep.allow_previous,
        previousReturnsToOffer: currentRouteKind === "detour"
          && !!app.commands.getGuideDetour?.(
            app.state.guides.currentGuide,
            app.state.guides.activeDetour?.id,
          )?.previous_returns_to_offer,
        suppressPrevious: !!app.state.guides.suppressPreviousForCurrentArrival,
      });
      previous.hidden = guidePreviousHidden({
        stepIndex: currentStepIndex,
        unavailable,
        previousReturnsToOffer: currentRouteKind === "detour"
          && !!app.commands.getGuideDetour?.(
            app.state.guides.currentGuide,
            app.state.guides.activeDetour?.id,
          )?.previous_returns_to_offer,
        suppressPrevious: !!app.state.guides.suppressPreviousForCurrentArrival,
      });
    }
    if (next) {
      next.hidden = unavailable
        || (
          currentStep.completion.kind === "event"
          && !app.state.guides.reviewingCompletedStep
          && !app.state.guides.completedStepIds.has(currentStep.id)
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
          && app.state.guides.completedStepIds.has(currentStep.id)
          ? "Action complete. Select Next to continue."
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
    if (!container) return;
    const detours = app.commands.getGuideDetoursAtCurrentStep?.() || [];
    container.hidden = detours.length === 0;
    container.dataset.layout = currentStep?.detour_layout || "default";
    container.replaceChildren?.(...detours.map(currentDetour => {
      const completed = app.state.guides.completedDetourIds.has(currentDetour.id);
      const visited = app.state.guides.visitedDetourIds?.has(currentDetour.id);
      const item = documentTarget.createElement("div");
      item.className = "guide-step-detour-item";
      const copy = documentTarget.createElement("div");
      copy.className = "guide-step-detour-copy";
      const label = documentTarget.createElement("span");
      label.className = "guide-step-detour-label";
      label.textContent = visited ? "Visited" : "Optional";
      const description = documentTarget.createElement("p");
      description.textContent = currentDetour.description;
      if (currentDetour.show_status !== false) copy.append(label);
      copy.append(description);
      const button = documentTarget.createElement("button");
      button.className = "ghost-button small";
      button.type = "button";
      button.textContent = completed && !currentDetour.repeatable
        ? "Complete"
        : currentDetour.button_label || `Explore ${currentDetour.label}`;
      button.disabled = completed && !currentDetour.repeatable;
      if (!button.disabled) button.dataset.detourId = currentDetour.id;
      if (currentStep?.detour_layout === "button-description") item.append(button, copy);
      else item.append(copy, button);
      return item;
    }));
  }

  function syncStepVisual() {
    const container = app.state.ui.$("#guideStepVisual");
    if (!container) return;
    const visual = currentStep?.visual;
    const source = visual?.clone_selector
      ? documentTarget.querySelector?.(visual.clone_selector)
      : null;
    if (typeof container.replaceChildren === "function") container.replaceChildren();
    else container.textContent = "";
    container.hidden = !source;
    if (!source) {
      container.removeAttribute("aria-label");
      return;
    }
    const clone = source.cloneNode(true);
    clone.removeAttribute("data-guide-visual");
    clone.setAttribute("aria-hidden", "true");
    container.setAttribute("aria-label", visual.label || "Guide illustration");
    container.appendChild(clone);
  }

  function syncStepNote() {
    const container = app.state.ui.$("#guideStepNote");
    const label = app.state.ui.$("#guideStepNoteLabel");
    const value = app.state.ui.$("#guideStepNoteValue");
    const note = currentStep?.note;
    if (container) container.hidden = !note;
    if (label) label.textContent = note?.label || "";
    if (value) value.textContent = note ? app.commands.formatGuideText(note.text) : "";
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
    card.dataset.size = step.card_size || "default";
    targetDeadline = now() + TARGET_WAIT_MS;
    if (!retainsPlacement) {
      lastCardPosition = null;
      lastCompanionPlacement = null;
    }
    app.state.guides.dockIndex = null;
    app.state.ui.$("#guideStepTitle").textContent = step.title;
    const body = app.state.ui.$("#guideStepBody");
    renderGuideTextContent(
      body,
      app.commands.formatGuideText(step.body),
      body?.ownerDocument || documentTarget,
    );
    syncStepNote();
    syncStepVisual();
    syncStepCompanion();
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
    if (!positionPairedSurfaces()) positionDockedCard();
  }

  function hideGuideOverlay({ restoreFocus = true } = {}) {
    if (!root) return;
    const focusWasInside = root.contains?.(documentTarget.activeElement);
    closeGuideMediaLightbox({ restoreFocus: false });
    companionRenderRevision += 1;
    if (companion) companion.hidden = true;
    companionContent?.replaceChildren?.();
    clearCompanionConstraints();
    root.classList.add("is-hidden");
    root.classList.remove("is-suspended");
    root.setAttribute("aria-hidden", "true");
    currentStep = null;
    currentGuide = null;
    lastCardPosition = null;
    lastCompanionPlacement = null;
    app.state.guides.dockIndex = null;
    mutationObserver?.disconnect();
    resizeObserver?.disconnect();
    observedTargets = [];
    observedCompanion = false;
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
    companion = app.state.ui.$("#guideCompanion");
    companionContent = app.state.ui.$("#guideCompanionContent");
    mediaLightbox = app.state.ui.$("#guideMediaLightbox");
    frames = [...app.state.ui.$$(".guide-target-frame")];
    shades = [...app.state.ui.$$(".guide-overlay-shade")];
    resizeObserver = typeof ResizeObserver === "function"
      ? new ResizeObserver((entries = []) => {
        if (entries.some(entry => entry.target !== companion)) {
          clearCompanionConstraints();
        }
        scheduleGuideOverlayLayout();
      })
      : null;
    mutationObserver = typeof MutationObserver === "function"
      ? new MutationObserver(() => scheduleGuideOverlayLayout())
      : null;
    app.lifecycle.listen(viewportTarget, "resize", () => {
      clearCompanionConstraints();
      scheduleGuideOverlayLayout();
    });
    app.lifecycle.listen(documentRoot, "scroll", () => scheduleGuideOverlayLayout(), true);
    if (companionContent) {
      app.lifecycle.listen(companionContent, "click", (event) => {
        const trigger = event.target?.closest?.("[data-guide-companion-item]");
        if (trigger) openGuideMediaLightbox(trigger.dataset.guideCompanionItem, trigger);
      });
    }
    const companionExpand = app.state.ui.$("#guideCompanionExpand");
    if (companionExpand) {
      app.lifecycle.listen(companionExpand, "click", () => {
        const itemIndex = companionExpand.dataset.guideCompanionItem;
        if (itemIndex !== undefined) openGuideMediaLightbox(itemIndex, companionExpand);
      });
    }
    const lightboxClose = app.state.ui.$("#guideMediaLightboxClose");
    if (lightboxClose) {
      app.lifecycle.listen(lightboxClose, "click", () => closeGuideMediaLightbox());
    }
    if (mediaLightbox) {
      app.lifecycle.listen(mediaLightbox, "click", (event) => {
        if (event.target === mediaLightbox) closeGuideMediaLightbox();
      });
    }
    if (viewportTarget.visualViewport) {
      app.lifecycle.listen(viewportTarget.visualViewport, "resize", () => {
        clearCompanionConstraints();
        scheduleGuideOverlayLayout();
      });
      app.lifecycle.listen(viewportTarget.visualViewport, "scroll", () => scheduleGuideOverlayLayout());
    }
    app.lifecycle.own(() => {
      closeGuideMediaLightbox({ restoreFocus: false });
      mutationObserver?.disconnect();
      resizeObserver?.disconnect();
    });
  }

  Object.assign(app.commands, {
    closeGuideMediaLightbox,
    hideGuideOverlay,
    initializeGuideOverlay,
    renderGuideStep,
    scheduleGuideOverlayLayout,
    showGuideTargetUnavailable,
  });
}
