function visibleItems(menu, selector) {
  return [...menu.querySelectorAll(selector)].filter(
    item => !item.hidden && !item.disabled && item.getAttribute("aria-disabled") !== "true",
  );
}

/** Install an accessible viewport-anchored menu and return its controller. */
export function createAnchoredMenuController({
  button,
  menu,
  onActivate,
  itemSelector = "[role='menuitem']",
  documentTarget = document,
  viewportTarget = window,
}) {
  const disposers = [];
  const listen = (target, type, listener, options) => {
    target.addEventListener(type, listener, options);
    disposers.push(() => target.removeEventListener(type, listener, options));
  };

  function position() {
    if (!button || !menu || menu.hidden) return;
    const rect = button.getBoundingClientRect();
    const viewport = viewportTarget.visualViewport;
    const leftEdge = viewport?.offsetLeft || 0;
    const topEdge = viewport?.offsetTop || 0;
    const width = viewport?.width || viewportTarget.innerWidth || documentTarget.documentElement.clientWidth;
    const height = viewport?.height || viewportTarget.innerHeight || documentTarget.documentElement.clientHeight;
    const rightEdge = leftEdge + width;
    const bottomEdge = topEdge + height;
    const margin = 8;
    const gap = 6;
    const left = Math.max(
      leftEdge + margin,
      Math.min(rect.right - menu.offsetWidth, rightEdge - menu.offsetWidth - margin),
    );
    const below = rect.bottom + gap;
    const top = below + menu.offsetHeight <= bottomEdge - margin
      ? below
      : Math.max(topEdge + margin, rect.top - menu.offsetHeight - gap);
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(top)}px`;
  }

  function close({ restoreFocus = false } = {}) {
    if (!menu || menu.hidden) return;
    menu.hidden = true;
    button?.setAttribute("aria-expanded", "false");
    if (restoreFocus) button?.focus();
  }

  function open({ focus = "first" } = {}) {
    if (!button || !menu) return;
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    position();
    const items = visibleItems(menu, itemSelector);
    if (!items.length || focus === "none") return;
    (focus === "last" ? items.at(-1) : items[0]).focus();
  }

  function toggle() {
    if (menu?.hidden) open(); else close({ restoreFocus: true });
  }

  function handleMenuKeydown(event) {
    const items = visibleItems(menu, itemSelector);
    if (!items.length) return;
    const currentIndex = Math.max(0, items.indexOf(event.target));
    let nextIndex = null;
    if (event.key === "ArrowDown") nextIndex = (currentIndex + 1) % items.length;
    if (event.key === "ArrowUp") nextIndex = (currentIndex - 1 + items.length) % items.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = items.length - 1;
    if (nextIndex !== null) {
      event.preventDefault();
      items[nextIndex].focus();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      close({ restoreFocus: true });
      return;
    }
    if (event.key === "Tab") close();
  }

  if (button) {
    listen(button, "click", toggle);
    listen(button, "keydown", event => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        open({ focus: event.key === "ArrowDown" ? "first" : "last" });
      } else if (event.key === "Escape") {
        close();
      }
    });
  }
  if (menu) {
    listen(menu, "click", event => {
      const item = event.target.closest?.(itemSelector);
      if (!item || item.hidden || item.disabled || item.getAttribute("aria-disabled") === "true") return;
      close({ restoreFocus: true });
      onActivate?.(item);
    });
    listen(menu, "keydown", handleMenuKeydown);
  }
  listen(documentTarget, "pointerdown", event => {
    if (menu?.hidden || menu?.contains(event.target) || button?.contains(event.target)) return;
    close();
  });
  listen(viewportTarget, "resize", () => close());
  listen(viewportTarget, "scroll", () => close());
  if (viewportTarget.visualViewport) {
    listen(viewportTarget.visualViewport, "resize", () => close());
    listen(viewportTarget.visualViewport, "scroll", () => close());
  }

  return Object.freeze({
    close,
    open,
    position,
    toggle,
    destroy() {
      for (const dispose of disposers.splice(0).reverse()) dispose();
    },
  });
}
