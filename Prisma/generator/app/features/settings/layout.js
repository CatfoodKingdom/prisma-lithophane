/**
 * Install the settings/layout feature commands.
 * @param {import("../../core/types.js").ApplicationContext} app
 */
export function installFeaturesSettingsLayout(app) {
function restoreSettingsFlowUnits(grid = document.querySelector(".settings-grid")) {
    if (!grid) return;
    const units = Array.from(grid.querySelectorAll("[data-settings-flow-owner]")).sort((a, b) => {
      const orderA = Number(a.dataset.settingsFlowOrder);
      const orderB = Number(b.dataset.settingsFlowOrder);
      if (Number.isFinite(orderA) && Number.isFinite(orderB) && orderA !== orderB) return orderA - orderB;
      return 0;
    });
    units.forEach((unit) => {
      const ownerId = unit.dataset.settingsFlowOwner;
      const owner = ownerId ? document.getElementById(ownerId) : null;
      if (!owner) return;
      if (unit.dataset.settingsFlowWrapper === "true") {
        const parentGroup = Array.from(unit.children)
          .find((child) => child.classList.contains("settings-group"));
        if (parentGroup) unit.before(parentGroup);
        while (unit.firstChild) {
          const child = unit.firstChild;
          child.removeAttribute?.("data-settings-parent-title");
          owner.appendChild(child);
        }
        unit.remove();
        return;
      }
      owner.appendChild(unit);
      unit.classList.remove("preprocessing-flow-unit");
      unit.removeAttribute("data-settings-flow-owner");
      unit.removeAttribute("data-settings-flow-order");
      unit.removeAttribute("data-settings-flow-group");
      unit.removeAttribute("data-bucket");
    });
  }

function extractSettingsSubsectionFlowUnits(grid) {
    const groups = Array.from(grid?.children || [])
      .filter((child) => child.classList.contains("settings-group"));

    groups.forEach((group, groupIndex) => {
      const groupKey = group.dataset.settingsGroup || group.dataset.bucket || `section-${groupIndex}`;
      if (!group.id) group.id = `settingsFlowOwner-${groupKey}`;

      const children = Array.from(group.children);
      const subsectionHeads = children
        .filter((child) => child.classList.contains("settings-subsection-head"));
      if (!subsectionHeads.length) return;

      const sectionHead = children.find((child) => child.classList.contains("settings-section-head"));
      const firstSubsectionIndex = children.indexOf(subsectionHeads[0]);
      const hasPreamble = children
        .slice(0, firstSubsectionIndex)
        .some((child) => child !== sectionHead);
      let insertionPoint = group;

      subsectionHeads.forEach((head, order) => {
        const wrapper = document.createElement("div");
        wrapper.className = "settings-subsection-flow-unit";
        wrapper.dataset.settingsFlowOwner = group.id;
        wrapper.dataset.settingsFlowOrder = String(order);
        wrapper.dataset.settingsFlowWrapper = "true";
        wrapper.dataset.settingsFlowGroup = groupKey;
        wrapper.dataset.bucket = group.dataset.bucket || groupKey;
        insertionPoint.after(wrapper);

        // A section with no introductory controls (for example White Cap)
        // keeps its title attached to its first subsection. Sections with a
        // preamble (for example Color Solver) may break before subsection one.
        if (order === 0 && !hasPreamble) wrapper.appendChild(group);

        let node = head;
        while (node) {
          const next = node.nextElementSibling;
          if (node !== head && node.classList.contains("settings-subsection-head")) break;
          wrapper.appendChild(node);
          node = next;
        }
        insertionPoint = wrapper;
      });
    });
    return grid;
}

function extractPreprocessingFlowUnits(grid) {
    const group = Array.from(grid?.children || [])
      .find((child) => child.dataset.settingsGroup === "preprocessing");
    const owner = document.getElementById("preprocessingSettingsContainer");
    if (!group || !owner) return grid;

    let insertionPoint = group;
    Array.from(owner.children)
      .filter((unit) => unit.classList.contains("module-settings-section"))
      .sort((a, b) => Number(a.dataset.settingsFlowOrder) - Number(b.dataset.settingsFlowOrder))
      .forEach((unit) => {
        unit.classList.add("preprocessing-flow-unit");
        unit.dataset.settingsFlowOwner = owner.id;
        unit.dataset.settingsFlowGroup = "preprocessing";
        unit.dataset.bucket = "preprocessing";
        insertionPoint.after(unit);
        insertionPoint = unit;
      });
    return grid;
  }

function partitionSettingsItems(items, heights, maxColumns, availableHeight, gap = 8) {
    const count = items.length;
    if (!count) return [];
    const totalHeight = heights.reduce((sum, height) => sum + height, 0)
      + (gap * Math.max(0, count - 1));
    const minimumColumnCount = Math.max(
      1,
      Math.min(maxColumns, count, Math.ceil(totalHeight / Math.max(1, availableHeight))),
    );

    const prefix = [0];
    heights.forEach((height) => prefix.push(prefix[prefix.length - 1] + height));
    const segmentHeight = (start, end) => (
      (prefix[end] - prefix[start]) + (gap * Math.max(0, end - start - 1))
    );

    const partitionForColumnCount = (columnCount) => {
      if (columnCount === 1) {
        return { columns: [items.slice()], tallest: totalHeight };
      }
      const best = Array.from({ length: columnCount + 1 }, () => (
        Array(count + 1).fill(Number.POSITIVE_INFINITY)
      ));
      const split = Array.from({ length: columnCount + 1 }, () => Array(count + 1).fill(-1));
      best[0][0] = 0;

      for (let columns = 1; columns <= columnCount; columns++) {
        for (let end = columns; end <= count; end++) {
          for (let start = columns - 1; start < end; start++) {
            const cost = Math.max(best[columns - 1][start], segmentHeight(start, end));
            if (cost < best[columns][end]) {
              best[columns][end] = cost;
              split[columns][end] = start;
            }
          }
        }
      }

      const ranges = [];
      let end = count;
      for (let columns = columnCount; columns > 0; columns--) {
        const start = split[columns][end];
        ranges.push([start, end]);
        end = start;
      }
      return {
        columns: ranges.reverse().map(([start, stop]) => items.slice(start, stop)),
        tallest: best[columnCount][count],
      };
    };

    // Total height only provides a lower bound. Atomic flow units can make that
    // many columns impossible to pack within the height budget even when their
    // combined height would fit mathematically. Add a column whenever no legal
    // contiguous split fits, provided the viewport has room for one.
    const allowedColumnCount = Math.max(1, Math.min(maxColumns, count));
    let result = partitionForColumnCount(minimumColumnCount);
    for (
      let columnCount = minimumColumnCount + 1;
      result.tallest > availableHeight && columnCount <= allowedColumnCount;
      columnCount++
    ) {
      result = partitionForColumnCount(columnCount);
    }
    return result.columns;
  }

function distributeSettingsColumns() {
    const grid = document.querySelector(".settings-grid");
    if (!grid) return;
    const inDrawer = grid.classList.contains("in-drawer");
    const colWidth = 380;
    const colGap = 24;
    const drawerMaxWidth = 1280;
    const drawerRevealWidth = 32;
    const drawerBodyPadding = 24;
    const drawerChrome = 2;
    // Abort if not measurable — unwrapping + re-running with clientWidth=0 would
    // collapse the grid to a single column and the result would stick once visible.
    if (grid.clientWidth < 100) return;

    // Reparenting a focused live control can make some browsers drop focus or
    // text selection. Preserve both across responsive redistribution.
    const focusedElement = grid.contains(document.activeElement) ? document.activeElement : null;
    const focusedSelection = focusedElement && typeof focusedElement.selectionStart === "number"
      ? {
          start: focusedElement.selectionStart,
          end: focusedElement.selectionEnd,
          direction: focusedElement.selectionDirection,
        }
      : null;

    const drawer = inDrawer ? document.getElementById("settingsDrawer") : null;
    if (drawer) {
      drawer.style.setProperty("--settings-drawer-width", "1280px");
    }

    // Restore canonical dynamic ownership before unwrapping and measuring.
    app.commands.restoreSettingsFlowUnits(grid);

    // Unwrap existing column wrappers first — move their children back to grid
    grid.querySelectorAll(".settings-column").forEach(col => {
      while (col.firstChild) grid.appendChild(col.firstChild);
      col.remove();
    });

    // Enabled preprocessing modules are independent atomic flow units. Extract
    // them before the general subsection pass so the Preprocessing heading,
    // resample control, and toggle table remain together as the base unit.
    app.commands.extractPreprocessingFlowUnits(grid);

    // Subsection headings are safe column-break opportunities. Each wrapper
    // keeps its heading attached to its controls, while restoreSettingsFlowUnits
    // returns the live controls to their canonical section before every reflow.
    app.commands.extractSettingsSubsectionFlowUnits(grid);

    const items = Array.from(grid.children);
    if (items.length === 0) return;

    // Calculate how many columns fit — fixed width matching drawer
    let availWidth = grid.clientWidth;
    if (inDrawer && drawer) {
      const hostWidth = drawer.parentElement?.clientWidth || window.innerWidth || availWidth;
      const targetDrawerWidth = Math.min(
        drawerMaxWidth,
        hostWidth <= 700 ? hostWidth : Math.max(420, hostWidth - drawerRevealWidth),
      );
      // During the slide animation the live grid can still measure at an intermediate width.
      // Plan columns against the intended drawer width, then shrink after layout.
      availWidth = Math.max(
        availWidth,
        targetDrawerWidth - drawerBodyPadding - drawerChrome,
      );
    }
    const maxCols = Math.max(1, Math.floor((availWidth + colGap) / (colWidth + colGap)));

    // Measure available height — the drawer body when open, else the
    // Settings tab (#tabSettings is 0-height while hidden, so prefer the live host).
    const measureEl = inDrawer
      ? document.getElementById("settingsDrawerBody")
      : document.getElementById("tabSettings");
    let availHeight = (measureEl ? measureEl.clientHeight : grid.parentElement.clientHeight) || 800;
    if (measureEl && inDrawer) {
      const measureStyle = getComputedStyle(measureEl);
      const verticalPadding =
        (parseFloat(measureStyle.paddingTop) || 0)
        + (parseFloat(measureStyle.paddingBottom) || 0);
      // clientHeight includes padding, but the columns live inside that padding.
      // Budget against the content box or the drawer gains a tiny
      // phantom scrollbar even when the visible columns fit.
      availHeight = Math.max(120, availHeight - verticalPadding);
    }

    // Measure each item's height
    const heights = items.map(el => el.getBoundingClientRect().height);

    // Keep the reading order contiguous while choosing the column breaks that
    // minimize the tallest column. This avoids a mostly empty early column and
    // a needless scrollbar caused by overflowing everything into the last one.
    let columns = app.commands.partitionSettingsItems(
      items,
      heights,
      maxCols,
      availHeight,
    );

    const mountColumns = (columnItems) => columnItems.map((colItems) => {
      const colDiv = document.createElement("div");
      colDiv.className = "settings-column";
      // Attach the destination before moving live controls into it. Reparenting
      // a focused input through a detached column can synchronously fire its
      // change handler while sibling settings (including #derivedParams) are
      // temporarily absent from document queries.
      grid.appendChild(colDiv);
      colItems.forEach(el => colDiv.appendChild(el));
      return colDiv;
    });
    let columnElements = mountColumns(columns);

    // Some controls acquire their final wrapped height only after entering a
    // fixed-width column. Repartition once from those rendered measurements so
    // the plan cannot retain a stale pre-column height estimate.
    if (columns.length > 1) {
      const renderedItems = columnElements.flatMap((column) => Array.from(column.children));
      const renderedHeights = renderedItems.map((item) => item.getBoundingClientRect().height);
      const renderedColumns = app.commands.partitionSettingsItems(
        renderedItems,
        renderedHeights,
        columns.length,
        availHeight,
      );
      columnElements.forEach((column) => {
        while (column.firstChild) grid.appendChild(column.firstChild);
        column.remove();
      });
      columns = renderedColumns;
      columnElements = mountColumns(columns);
    }

    if (focusedElement && grid.contains(focusedElement)) {
      focusedElement.focus({ preventScroll: true });
      if (focusedSelection && typeof focusedElement.setSelectionRange === "function") {
        focusedElement.setSelectionRange(
          focusedSelection.start,
          focusedSelection.end,
          focusedSelection.direction,
        );
      }
    }

    if (inDrawer) {
      const usedColumns = Math.max(1, columns.length);
      const contentWidth = (usedColumns * colWidth) + ((usedColumns - 1) * colGap);
      drawer?.style.setProperty(
        "--settings-drawer-width",
        `${contentWidth + drawerBodyPadding + drawerChrome}px`,
      );
    }
  }

function initCollapsibleSections() {
    // Retained as a no-op command for startup callers and guide integrations.
    // Section headers are now informational and never collapse.
}

  Object.assign(app.commands, {
    restoreSettingsFlowUnits,
    extractSettingsSubsectionFlowUnits,
    extractPreprocessingFlowUnits,
    partitionSettingsItems,
    distributeSettingsColumns,
    initCollapsibleSections,
  });
}
