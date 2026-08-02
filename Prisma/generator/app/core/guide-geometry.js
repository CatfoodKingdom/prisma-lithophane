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

  const dock = GUIDE_DOCKS[0];
  return { ...dockPosition(dock, card, viewport, margin), placement: `dock-${dock}`, docked: true };
}
