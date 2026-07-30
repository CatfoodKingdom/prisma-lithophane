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
      unit.removeAttribute("data-bucket");
    });
  }

function extractSettingsSubsectionFlowUnits(grid) {
    const groups = Array.from(grid?.querySelectorAll(".settings-group.is-expanded") || []);
    groups.forEach((group) => {
      const body = Array.from(group.children).find((child) => child.classList.contains("section-collapse-body"));
      if (!body) return;
      if (!body.id) {
        const key = group.dataset.settingsGroup || group.dataset.bucket || "section";
        body.id = `settingsFlowOwner-${key}`;
      }

      const parentTitle = group.querySelector(".section-collapse-title")?.textContent?.trim()
        || group.dataset.settingsGroup
        || "Settings";
      const heads = Array.from(body.children).filter((child) => child.classList.contains("settings-subsection-head"));
      let insertionPoint = group;

      heads.forEach((head, order) => {
        const wrapper = document.createElement("div");
        wrapper.className = "settings-subsection-flow-unit";
        wrapper.dataset.settingsFlowOwner = body.id;
        wrapper.dataset.settingsFlowOrder = String(order);
        wrapper.dataset.settingsFlowWrapper = "true";
        wrapper.dataset.bucket = group.dataset.bucket || group.dataset.settingsGroup || "settings";
        insertionPoint.after(wrapper);
        if (order === 0) {
          wrapper.appendChild(group);
        } else {
          head.dataset.settingsParentTitle = parentTitle;
        }

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
  }

function extractPreprocessingFlowUnits(grid) {
    const group = grid?.querySelector('[data-settings-group="preprocessing"]');
    const owner = document.getElementById("preprocessingSettingsContainer");
    if (!group || !owner || !group.classList.contains("is-expanded")) return;

    let insertionPoint = group;
    Array.from(owner.children)
      .filter((unit) => unit.classList.contains("module-settings-section"))
      .sort((a, b) => Number(a.dataset.settingsFlowOrder) - Number(b.dataset.settingsFlowOrder))
      .forEach((unit) => {
      unit.classList.add("preprocessing-flow-unit");
      unit.dataset.settingsFlowOwner = owner.id;
      unit.dataset.bucket = "preprocessing";
      insertionPoint.after(unit);
      insertionPoint = unit;
      });
  }

function partitionSettingsItems(items, heights, maxColumns, availableHeight, gap = 8) {
    const count = items.length;
    if (!count) return [];
    const totalHeight = heights.reduce((sum, height) => sum + height, 0)
      + (gap * Math.max(0, count - 1));
    const columnCount = Math.max(
      1,
      Math.min(maxColumns, count, Math.ceil(totalHeight / Math.max(1, availableHeight))),
    );
    if (columnCount === 1) return [items.slice()];

    const prefix = [0];
    heights.forEach((height) => prefix.push(prefix[prefix.length - 1] + height));
    const segmentHeight = (start, end) => (
      (prefix[end] - prefix[start]) + (gap * Math.max(0, end - start - 1))
    );
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
    return ranges.reverse().map(([start, stop]) => items.slice(start, stop));
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

    // Enabled preprocessing cards must leave their canonical subsection before
    // the parent group is wrapped with its first static subsection.
    app.commands.extractPreprocessingFlowUnits(grid);
    // Static subsections and enabled preprocessing module cards are independent
    // layout units. Moving the live nodes lets them flow across columns without
    // splitting a heading from its controls or losing input state and handlers.
    app.commands.extractSettingsSubsectionFlowUnits(grid);

    // All items participate in column distribution — no exceptions
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
    document.querySelectorAll(".settings-grid .settings-group").forEach(group => {
      const h4 = group.querySelector(".settings-section-head");
      if (!h4) return;

      // Build header bar
      const header = document.createElement("div");
      header.className = "section-collapse-header";
      header.setAttribute("role", "button");
      header.setAttribute("tabindex", "0");
      header.setAttribute("aria-expanded", "true");
      const arrow = document.createElement("span");
      arrow.className = "section-collapse-arrow";
      arrow.setAttribute("aria-hidden", "true");
      const title = document.createElement("span");
      title.className = "section-collapse-title";
      title.textContent = h4.textContent;
      header.appendChild(arrow);
      header.appendChild(title);

      // Wrap remaining content in a body div
      const body = document.createElement("div");
      body.className = "section-collapse-body";
      // Move all children except h4 into body
      while (group.children.length > 0) {
        const child = group.children[0];
        if (child === h4) { h4.remove(); continue; }
        body.appendChild(child);
      }

      group.appendChild(header);
      group.appendChild(body);
      group.classList.add("is-collapsible", "is-expanded");

      const setExpanded = (expanded) => {
        group.classList.toggle("is-expanded", expanded);
        header.setAttribute("aria-expanded", expanded ? "true" : "false");
        body.classList.toggle("is-hidden", !expanded);
        app.commands.distributeSettingsColumns();
      };

      header.addEventListener("click", () => {
        const expanded = group.classList.toggle("is-expanded");
        setExpanded(expanded);
      });
      header.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        setExpanded(!group.classList.contains("is-expanded"));
      });
    });
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
