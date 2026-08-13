export function normalizeRect(rect) {
  const left = Number(rect?.left) || 0;
  const top = Number(rect?.top) || 0;
  const right = Number.isFinite(Number(rect?.right))
    ? Number(rect.right)
    : left + (Number(rect?.width) || 0);
  const bottom = Number.isFinite(Number(rect?.bottom))
    ? Number(rect.bottom)
    : top + (Number(rect?.height) || 0);
  return {
    left,
    top,
    right: Math.max(left, right),
    bottom: Math.max(top, bottom),
    width: Math.max(0, right - left),
    height: Math.max(0, bottom - top),
  };
}

/**
 * Align a rectangle to CSS pixels by rounding its shared edges rather than
 * rounding its origin and dimensions independently. The latter can leave a
 * one-pixel gap between adjacent guide scrim rectangles at fractional layout
 * coordinates.
 */
export function alignRectToCssPixels(rect) {
  const normalized = normalizeRect(rect);
  const left = Math.round(normalized.left);
  const top = Math.round(normalized.top);
  const right = Math.max(left, Math.round(normalized.right));
  const bottom = Math.max(top, Math.round(normalized.bottom));
  return {
    left,
    top,
    right,
    bottom,
    width: right - left,
    height: bottom - top,
  };
}

export function intersectRects(first, second) {
  const a = normalizeRect(first);
  const b = normalizeRect(second);
  const left = Math.max(a.left, b.left);
  const top = Math.max(a.top, b.top);
  const right = Math.min(a.right, b.right);
  const bottom = Math.min(a.bottom, b.bottom);
  if (right <= left || bottom <= top) return null;
  return { left, top, right, bottom, width: right - left, height: bottom - top };
}

export function shadeRects(targetRect, viewportRect) {
  const viewport = normalizeRect(viewportRect);
  const target = intersectRects(targetRect, viewport);
  if (!target) {
    return [{ ...viewport }];
  }
  return [
    {
      left: viewport.left,
      top: viewport.top,
      width: viewport.width,
      height: Math.max(0, target.top - viewport.top),
    },
    {
      left: viewport.left,
      top: target.bottom,
      width: viewport.width,
      height: Math.max(0, viewport.bottom - target.bottom),
    },
    {
      left: viewport.left,
      top: target.top,
      width: Math.max(0, target.left - viewport.left),
      height: target.height,
    },
    {
      left: target.right,
      top: target.top,
      width: Math.max(0, viewport.right - target.right),
      height: target.height,
    },
  ];
}

/**
 * Partition the viewport into shade rectangles around one or more rectangular
 * spotlight openings. The sweep-line partition keeps disjoint targets clear
 * without exposing the unrelated space between them.
 */
export function shadeRectsAroundTargets(targetRects, viewportRect) {
  const viewport = normalizeRect(viewportRect);
  const targets = (targetRects || [])
    .map(rect => intersectRects(rect, viewport))
    .filter(Boolean);
  if (targets.length === 0) return [{ ...viewport }];
  if (targets.length === 1) return shadeRects(targets[0], viewport);

  const yBoundaries = [
    viewport.top,
    viewport.bottom,
    ...targets.flatMap(target => [target.top, target.bottom]),
  ]
    .filter(value => value >= viewport.top && value <= viewport.bottom)
    .sort((left, right) => left - right)
    .filter((value, index, values) => index === 0 || value !== values[index - 1]);
  const shaded = [];

  for (let index = 0; index < yBoundaries.length - 1; index += 1) {
    const top = yBoundaries[index];
    const bottom = yBoundaries[index + 1];
    if (bottom <= top) continue;
    const openings = targets
      .filter(target => target.top < bottom && target.bottom > top)
      .map(target => [target.left, target.right])
      .sort((left, right) => left[0] - right[0]);
    const merged = [];
    for (const opening of openings) {
      const previous = merged.at(-1);
      if (previous && opening[0] <= previous[1]) {
        previous[1] = Math.max(previous[1], opening[1]);
      } else {
        merged.push([...opening]);
      }
    }

    let left = viewport.left;
    for (const [openingLeft, openingRight] of merged) {
      if (openingLeft > left) {
        shaded.push(normalizeRect({ left, top, right: openingLeft, bottom }));
      }
      left = Math.max(left, openingRight);
    }
    if (left < viewport.right) {
      shaded.push(normalizeRect({ left, top, right: viewport.right, bottom }));
    }
  }
  return shaded;
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(value, maximum));
}

function cardFits(position, card, viewport, margin) {
  return (
    position.left >= viewport.left + margin
    && position.top >= viewport.top + margin
    && position.left + card.width <= viewport.right - margin
    && position.top + card.height <= viewport.bottom - margin
  );
}

function rectsOverlap(first, second) {
  const a = normalizeRect(first);
  const b = normalizeRect(second);
  return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
}

function retainedCardPosition(position, card, viewport, avoidRects, gap, margin) {
  if (!position || !Number.isFinite(Number(position.left)) || !Number.isFinite(Number(position.top))) {
    return null;
  }
  const left = clamp(
    Number(position.left),
    viewport.left + margin,
    viewport.right - card.width - margin,
  );
  const top = clamp(
    Number(position.top),
    viewport.top + margin,
    viewport.bottom - card.height - margin,
  );
  const cardRect = normalizeRect({
    left,
    top,
    right: left + card.width,
    bottom: top + card.height,
  });
  const obscuresTarget = (avoidRects || []).some(rawRect => {
    const rect = normalizeRect(rawRect);
    return rectsOverlap(cardRect, {
      left: rect.left - gap,
      top: rect.top - gap,
      right: rect.right + gap,
      bottom: rect.bottom + gap,
    });
  });
  return obscuresTarget ? null : { left, top };
}

export const GUIDE_DOCKS = Object.freeze([
  "bottom-right",
  "bottom-left",
  "top-right",
  "top-left",
]);

function dockPosition(name, card, viewport, margin) {
  const right = viewport.right - card.width - margin;
  const bottom = viewport.bottom - card.height - margin;
  if (name === "bottom-left") return { left: viewport.left + margin, top: bottom };
  if (name === "top-right") return { left: right, top: viewport.top + margin };
  if (name === "top-left") return { left: viewport.left + margin, top: viewport.top + margin };
  return { left: right, top: bottom };
}

function overlapArea(first, second) {
  const overlap = intersectRects(first, second);
  return overlap ? overlap.width * overlap.height : 0;
}

function leastObstructiveDock(card, viewport, avoidRects, gap, margin) {
  let best = null;
  for (const name of GUIDE_DOCKS) {
    const position = dockPosition(name, card, viewport, margin);
    const cardRect = normalizeRect({
      left: position.left,
      top: position.top,
      right: position.left + card.width,
      bottom: position.top + card.height,
    });
    const obstruction = (avoidRects || []).reduce((total, rawRect) => {
      const rect = normalizeRect(rawRect);
      return total + overlapArea(cardRect, {
        left: rect.left - gap,
        top: rect.top - gap,
        right: rect.right + gap,
        bottom: rect.bottom + gap,
      });
    }, 0);
    if (!best || obstruction < best.obstruction) {
      best = { name, position, obstruction };
    }
  }
  return best;
}

export function chooseGuideCardPlacement({
  targetRect,
  cardSize,
  viewportRect,
  preferred = ["bottom", "right", "top", "left"],
  dockIndex = null,
  previousPosition = null,
  avoidRects = null,
  gap = 10,
  margin = 8,
}) {
  const target = normalizeRect(targetRect);
  const viewport = normalizeRect(viewportRect);
  const card = {
    width: Math.min(Math.max(1, Number(cardSize?.width) || 1), Math.max(1, viewport.width - 2 * margin)),
    height: Math.min(Math.max(1, Number(cardSize?.height) || 1), Math.max(1, viewport.height - 2 * margin)),
  };

  if (Number.isInteger(dockIndex)) {
    const dock = GUIDE_DOCKS[((dockIndex % GUIDE_DOCKS.length) + GUIDE_DOCKS.length) % GUIDE_DOCKS.length];
    return { ...dockPosition(dock, card, viewport, margin), placement: `dock-${dock}`, docked: true };
  }

  const retained = retainedCardPosition(
    previousPosition,
    card,
    viewport,
    avoidRects || [target],
    gap,
    margin,
  );
  if (retained) {
    return { ...retained, placement: "retained", docked: false };
  }

  const centeredLeft = clamp(
    target.left + (target.width - card.width) / 2,
    viewport.left + margin,
    viewport.right - card.width - margin,
  );
  const centeredTop = clamp(
    target.top + (target.height - card.height) / 2,
    viewport.top + margin,
    viewport.bottom - card.height - margin,
  );
  const candidates = {
    bottom: { left: centeredLeft, top: target.bottom + gap },
    top: { left: centeredLeft, top: target.top - card.height - gap },
    right: { left: target.right + gap, top: centeredTop },
    left: { left: target.left - card.width - gap, top: centeredTop },
  };
  for (const name of preferred) {
    const candidate = candidates[name];
    if (candidate && cardFits(candidate, card, viewport, margin)) {
      return { ...candidate, placement: name, docked: false };
    }
  }

  const fallback = leastObstructiveDock(card, viewport, avoidRects || [target], gap, margin);
  return {
    ...fallback.position,
    placement: `dock-${fallback.name}`,
    docked: true,
  };
}

function sizedRect(position, size) {
  return normalizeRect({
    left: position.left,
    top: position.top,
    right: position.left + size.width,
    bottom: position.top + size.height,
  });
}

function surfaceAvoids(rect, avoidRects) {
  return !(avoidRects || []).some(candidate => rectsOverlap(rect, candidate));
}

function companionPositions(cardRect, companion, side, gap) {
  const positions = [];
  if (side === "top" || side === "bottom") {
    const top = side === "top"
      ? cardRect.top - companion.height - gap
      : cardRect.bottom + gap;
    for (const [alignment, left] of [
      ["start", cardRect.left],
      ["center", cardRect.left + ((cardRect.width - companion.width) / 2)],
      ["end", cardRect.right - companion.width],
    ]) positions.push({ left, top, side, alignment });
  } else {
    const left = side === "left"
      ? cardRect.left - companion.width - gap
      : cardRect.right + gap;
    for (const [alignment, top] of [
      ["start", cardRect.top],
      ["center", cardRect.top + ((cardRect.height - companion.height) / 2)],
      ["end", cardRect.bottom - companion.height],
    ]) positions.push({ left, top, side, alignment });
  }
  return positions;
}

function pairAtAnchor({ card, companion, side, alignment, viewport, anchor, gap, margin }) {
  const horizontal = side === "left" || side === "right";
  const pairWidth = horizontal ? card.width + gap + companion.width : Math.max(card.width, companion.width);
  const pairHeight = horizontal ? Math.max(card.height, companion.height) : card.height + gap + companion.height;
  const horizontalFraction = anchor === "center-left" ? 0.25 : anchor === "center-right" ? 0.67 : 0.5;
  const pairLeft = clamp(
    viewport.left + (viewport.width * horizontalFraction) - (pairWidth / 2),
    viewport.left + margin,
    viewport.right - pairWidth - margin,
  );
  const pairTop = clamp(
    viewport.top + ((viewport.height - pairHeight) / 2),
    viewport.top + margin,
    viewport.bottom - pairHeight - margin,
  );
  let cardLeft = pairLeft;
  let cardTop = pairTop;
  let companionLeft = pairLeft;
  let companionTop = pairTop;

  if (horizontal) {
    if (side === "left") {
      companionLeft = pairLeft;
      cardLeft = pairLeft + companion.width + gap;
    } else {
      cardLeft = pairLeft;
      companionLeft = pairLeft + card.width + gap;
    }
    if (alignment === "center") companionTop += (pairHeight - companion.height) / 2;
    if (alignment === "end") companionTop += pairHeight - companion.height;
    if (alignment === "center") cardTop += (pairHeight - card.height) / 2;
    if (alignment === "end") cardTop += pairHeight - card.height;
  } else {
    if (side === "top") {
      companionTop = pairTop;
      cardTop = pairTop + companion.height + gap;
    } else {
      cardTop = pairTop;
      companionTop = pairTop + card.height + gap;
    }
    if (alignment === "center") companionLeft += (pairWidth - companion.width) / 2;
    if (alignment === "end") companionLeft += pairWidth - companion.width;
    if (alignment === "center") cardLeft += (pairWidth - card.width) / 2;
    if (alignment === "end") cardLeft += pairWidth - card.width;
  }
  return {
    card: { left: cardLeft, top: cardTop },
    companion: { left: companionLeft, top: companionTop, side, alignment },
  };
}

function validSurfacePair(candidate, card, companion, viewport, avoidRects, margin) {
  const cardRect = sizedRect(candidate.card, card);
  const companionRect = sizedRect(candidate.companion, companion);
  return cardFits(candidate.card, card, viewport, margin)
    && cardFits(candidate.companion, companion, viewport, margin)
    && !rectsOverlap(cardRect, companionRect)
    && surfaceAvoids(cardRect, avoidRects)
    && surfaceAvoids(companionRect, avoidRects);
}

function anchoredCardPosition(anchor, card, viewport, margin) {
  const horizontalFraction = anchor === "center-left" ? 0.25 : anchor === "center-right" ? 0.67 : 0.5;
  return {
    left: clamp(
      viewport.left + (viewport.width * horizontalFraction) - (card.width / 2),
      viewport.left + margin,
      viewport.right - card.width - margin,
    ),
    top: clamp(
      viewport.top + ((viewport.height - card.height) / 2),
      viewport.top + margin,
      viewport.bottom - card.height - margin,
    ),
  };
}

/**
 * Jointly place the Guide card and its optional companion. This preserves the
 * existing single-card solver while making targetless, anchored, retained,
 * and docked paired layouts explicit and testable.
 */
export function chooseGuideSurfaceLayout({
  targetRect = null,
  cardSize,
  companionSize,
  viewportRect,
  preferred = ["bottom", "right", "top", "left"],
  companionPreferred = ["right", "left", "bottom", "top"],
  dockIndex = null,
  viewportAnchor = null,
  previousPosition = null,
  previousCompanionPlacement = null,
  avoidRects = [],
  gap = 10,
  margin = 8,
}) {
  const viewport = normalizeRect(viewportRect);
  const card = {
    width: Math.min(Math.max(1, Number(cardSize?.width) || 1), Math.max(1, viewport.width - (2 * margin))),
    height: Math.min(Math.max(1, Number(cardSize?.height) || 1), Math.max(1, viewport.height - (2 * margin))),
  };
  const companion = {
    width: Math.min(Math.max(1, Number(companionSize?.width) || 1), Math.max(1, viewport.width - (2 * margin))),
    height: Math.min(Math.max(1, Number(companionSize?.height) || 1), Math.max(1, viewport.height - (2 * margin))),
  };
  const target = targetRect == null ? null : normalizeRect(targetRect);
  const candidates = [];
  const addCompanionCandidates = (cardPosition, cardPlacement, cardRank) => {
    const cardRect = sizedRect(cardPosition, card);
    for (const [sideRank, side] of companionPreferred.entries()) {
      for (const position of companionPositions(cardRect, companion, side, gap)) {
        const retained = previousCompanionPlacement
          && previousCompanionPlacement.side === side
          && previousCompanionPlacement.alignment === position.alignment;
        candidates.push({
          card: { ...cardPosition, placement: cardPlacement },
          companion: position,
          score: [cardRank, retained ? 0 : 1, sideRank],
        });
      }
    }
  };

  if (Number.isInteger(dockIndex)) {
    const dock = GUIDE_DOCKS[((dockIndex % GUIDE_DOCKS.length) + GUIDE_DOCKS.length) % GUIDE_DOCKS.length];
    addCompanionCandidates(dockPosition(dock, card, viewport, margin), `dock-${dock}`, 0);
  } else if (!target) {
    const anchor = viewportAnchor || "center";
    if (previousPosition) addCompanionCandidates(previousPosition, "retained", 0);
    for (const [sideRank, side] of companionPreferred.entries()) {
      for (const alignment of ["start", "center", "end"]) {
        const pair = pairAtAnchor({ card, companion, side, alignment, viewport, anchor, gap, margin });
        candidates.push({
          card: { ...pair.card, placement: `viewport-${anchor}` },
          companion: pair.companion,
          score: [1, sideRank, alignment === "center" ? 0 : alignment === "start" ? 1 : 2],
        });
      }
    }
  } else {
    if (previousPosition) addCompanionCandidates(previousPosition, "retained", 0);
    if (viewportAnchor) addCompanionCandidates(
      anchoredCardPosition(viewportAnchor, card, viewport, margin),
      `viewport-${viewportAnchor}`,
      1,
    );
    const centeredLeft = clamp(
      target.left + ((target.width - card.width) / 2),
      viewport.left + margin,
      viewport.right - card.width - margin,
    );
    const centeredTop = clamp(
      target.top + ((target.height - card.height) / 2),
      viewport.top + margin,
      viewport.bottom - card.height - margin,
    );
    const positions = {
      bottom: { left: centeredLeft, top: target.bottom + gap },
      top: { left: centeredLeft, top: target.top - card.height - gap },
      right: { left: target.right + gap, top: centeredTop },
      left: { left: target.left - card.width - gap, top: centeredTop },
    };
    preferred.forEach((name, index) => {
      if (positions[name]) addCompanionCandidates(positions[name], name, 2 + index);
    });
    GUIDE_DOCKS.forEach((dock, index) => {
      addCompanionCandidates(dockPosition(dock, card, viewport, margin), `dock-${dock}`, 20 + index);
    });
  }

  const valid = candidates
    .filter(candidate => validSurfacePair(candidate, card, companion, viewport, avoidRects, margin))
    .sort((left, right) => {
      for (let index = 0; index < Math.max(left.score.length, right.score.length); index += 1) {
        const difference = (left.score[index] || 0) - (right.score[index] || 0);
        if (difference) return difference;
      }
      return 0;
    })[0];
  if (valid) {
    return {
      card: valid.card,
      companion: {
        ...valid.companion,
        placement: `${valid.companion.side}-${valid.companion.alignment}`,
      },
      companionConstraints: null,
    };
  }

  const fallbackCard = Number.isInteger(dockIndex)
    ? dockPosition(
      GUIDE_DOCKS[((dockIndex % GUIDE_DOCKS.length) + GUIDE_DOCKS.length) % GUIDE_DOCKS.length],
      card,
      viewport,
      margin,
    )
    : target
      ? chooseGuideCardPlacement({
        targetRect: target,
        cardSize: card,
        viewportRect: viewport,
        preferred,
        previousPosition,
        avoidRects,
        gap,
        margin,
      })
      : anchoredCardPosition(viewportAnchor || "center", card, viewport, margin);
  const fallbackRect = sizedRect(fallbackCard, card);
  for (const side of companionPreferred) {
    const maxWidth = side === "left"
      ? fallbackRect.left - viewport.left - margin - gap
      : side === "right"
        ? viewport.right - margin - fallbackRect.right - gap
        : viewport.width - (2 * margin);
    const maxHeight = side === "top"
      ? fallbackRect.top - viewport.top - margin - gap
      : side === "bottom"
        ? viewport.bottom - margin - fallbackRect.bottom - gap
        : viewport.height - (2 * margin);
    if (maxWidth < 1 || maxHeight < 1) continue;
    const constrained = {
      width: Math.min(companion.width, maxWidth),
      height: Math.min(companion.height, maxHeight),
    };
    const position = companionPositions(fallbackRect, constrained, side, gap)
      .find(candidate => validSurfacePair(
        { card: fallbackCard, companion: candidate },
        card,
        constrained,
        viewport,
        avoidRects,
        margin,
      ));
    if (position) {
      return {
        card: { ...fallbackCard, placement: fallbackCard.placement || "fallback" },
        companion: { ...position, placement: `${position.side}-${position.alignment}` },
        companionConstraints: { maxWidth, maxHeight },
      };
    }
  }

  return null;
}
