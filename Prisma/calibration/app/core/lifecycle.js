export function createLifecycle() {
  const disposers = [];
  let disposed = false;
  return {
    listen(target, type, listener, options) {
      if (!target || disposed) return;
      target.addEventListener(type, listener, options);
      disposers.push(() => target.removeEventListener(type, listener, options));
    },
    own(disposer) {
      if (!disposed && typeof disposer === "function") disposers.push(disposer);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      while (disposers.length) {
        try {
          disposers.pop()();
        } catch {}
      }
    },
    get disposed() {
      return disposed;
    },
  };
}
