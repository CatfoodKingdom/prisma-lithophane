/** Install core/ui commands. */
export function installCoreUi(app) {
  function bindConfirmAction(
    el,
    { onConfirm, armedText = "confirm?", timeout = 3000 } = {},
  ) {
    let armed = false;
    let timer = null;
    const originalHtml = el.innerHTML;
    const originalClass = el.className;

    function reset() {
      armed = false;
      el.innerHTML = originalHtml;
      el.className = originalClass;
      clearTimeout(timer);
    }

    el.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      if (el.disabled) return;
      if (!armed) {
        armed = true;
        el.textContent = armedText;
        el.classList.add("is-armed");
        timer = setTimeout(reset, timeout);
      } else {
        reset();
        if (onConfirm) await onConfirm();
      }
    });
  }

  function renderWindowCloseButton({
    id = "",
    className = "",
    label = "Close dialog",
    title = "Close dialog",
    disabled = false,
    attributes = "",
  } = {}) {
    const idAttr = id ? ` id="${app.commands.escapeHtml(id)}"` : "";
    const extraClass = className
      ? ` ${app.commands.escapeHtml(className)}`
      : "";
    const disabledAttr = disabled ? " disabled" : "";
    const extraAttrs = attributes ? ` ${attributes}` : "";
    return `
      <button class="close-button small window-close-button${extraClass}" type="button"${idAttr} aria-label="${app.commands.escapeHtml(label)}" title="${app.commands.escapeHtml(title)}"${disabledAttr}${extraAttrs}>
        <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
          <path d="M2 2 L10 10 M10 2 L2 10"></path>
        </svg>
      </button>
    `;
  }

  function renderWindowControls({
    closeButtonHtml = "",
    extraControlsHtml = "",
    className = "",
  } = {}) {
    if (!closeButtonHtml && !extraControlsHtml) return "";
    const extraClass = className
      ? ` ${app.commands.escapeHtml(className)}`
      : "";
    return `
      <div class="drawer-window-controls dialog-window-controls${extraClass}">
        ${extraControlsHtml || ""}
        ${closeButtonHtml || ""}
      </div>
    `;
  }

  function renderDialogHeader({
    title,
    titleId = "",
    subtitle = "",
    actionsHtml = "",
    closeButtonHtml = "",
    extraControlsHtml = "",
    headerClass = "",
    titleClass = "",
  } = {}) {
    const titleIdAttr = titleId
      ? ` id="${app.commands.escapeHtml(titleId)}"`
      : "";
    const headerClassAttr = headerClass
      ? ` ${app.commands.escapeHtml(headerClass)}`
      : "";
    const titleClassAttr = titleClass
      ? ` class="${app.commands.escapeHtml(titleClass)}"`
      : "";
    const actions = actionsHtml
      ? `<div class="dialog-header-actions">${actionsHtml}</div>`
      : "";
    const controls = app.commands.renderWindowControls({
      closeButtonHtml,
      extraControlsHtml,
    });
    const toolbar =
      actions || controls
        ? `<div class="dialog-header-toolbar">${actions}${controls}</div>`
        : "";
    return `
      <div class="info-dialog-header${headerClassAttr}">
        <div class="info-dialog-title-block">
          <h3${titleIdAttr}${titleClassAttr}>${app.commands.escapeHtml(title || "")}</h3>
          ${subtitle ? `<div class="info-dialog-subtitle">${app.commands.escapeHtml(subtitle)}</div>` : ""}
        </div>
        ${toolbar}
      </div>
    `;
  }

  Object.assign(app.commands, {
    bindConfirmAction,
    renderWindowCloseButton,
    renderWindowControls,
    renderDialogHeader,
  });
}
