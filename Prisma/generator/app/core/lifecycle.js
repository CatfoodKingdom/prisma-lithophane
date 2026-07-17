/** Own event listeners and timers so a feature can be disposed without leaks. */
export function createLifecycle() {
  const disposers = [];
  let disposed = false;

  function listen(target, type, listener, options) {
    if (disposed) throw new Error("Cannot bind through a disposed lifecycle");
    target.addEventListener(type, listener, options);
    disposers.push(() => target.removeEventListener(type, listener, options));
  }

  function timeout(callback, delayMs) {
    if (disposed) return null;
    const id = setTimeout(callback, delayMs);
    disposers.push(() => clearTimeout(id));
    return id;
  }

  function own(disposer) {
    if (disposed) {
      disposer();
      return;
    }
    disposers.push(disposer);
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    for (const disposer of disposers.splice(0).reverse()) disposer();
  }

  return Object.freeze({ listen, timeout, own, dispose, get disposed() { return disposed; } });
}
