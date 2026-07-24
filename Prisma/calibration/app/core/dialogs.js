/** Install reusable dialog ownership primitives. */
export function installCoreDialogs(app) {
  function createBusyDialogGuard({ element, onClose = () => {} } = {}) {
    let busy = false;
    let closed = false;
    return {
      setBusy(value) {
        if (!closed) busy = Boolean(value);
      },
      close({ force = false } = {}) {
        if (closed) return true;
        if (busy && !force) return false;
        closed = true;
        element?.remove();
        onClose();
        return true;
      },
      get busy() {
        return busy;
      },
      get closed() {
        return closed;
      },
    };
  }

  Object.assign(app.commands, { createBusyDialogGuard });
}
