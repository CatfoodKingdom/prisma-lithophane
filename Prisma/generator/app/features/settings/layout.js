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
      owner.appendChild(unit);
      unit.classList.remove("preprocessing-flow-unit");
      unit.removeAttribute("data-settings-flow-owner");
      unit.removeAttribute("data-bucket");
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

    // Enabled preprocessing module cards are independent layout units. Moving
    // the live nodes lets them flow across columns without splitting a card or
    // losing input values, event handlers, focus, or slider state.
    app.commands.extractPreprocessingFlowUnits(grid);

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

    // Fill each column top-to-bottom before starting the next
    const columns = [];
    let colIdx = 0;
    let colHeight = 0;

    for (let i = 0; i < items.length; i++) {
      if (colIdx >= maxCols) colIdx = maxCols - 1; // overflow into last column
      if (!columns[colIdx]) columns[colIdx] = [];

      // If this column is non-empty and adding this item would overflow, start next column
      if (columns[colIdx].length > 0 && colHeight + heights[i] > availHeight && colIdx < maxCols - 1) {
        colIdx++;
        colHeight = 0;
        if (!columns[colIdx]) columns[colIdx] = [];
      }

      columns[colIdx].push(items[i]);
      colHeight += heights[i] + 8; // 8px gap
    }

    // Build column divs and move items into them
    columns.forEach(colItems => {
      const colDiv = document.createElement("div");
      colDiv.className = "settings-column";
      // Attach the destination before moving live controls into it. Reparenting
      // a focused input through a detached column can synchronously fire its
      // change handler while sibling settings (including #derivedParams) are
      // temporarily absent from document queries.
      grid.appendChild(colDiv);
      colItems.forEach(el => colDiv.appendChild(el));
    });

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
    extractPreprocessingFlowUnits,
    distributeSettingsColumns,
    initCollapsibleSections,
  });
}
