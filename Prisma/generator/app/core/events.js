/** Create a small synchronous event bus for semantic cross-feature events. */
export function createEventBus() {
  const listeners = new Map();

  function subscribe(type, listener) {
    if (!type || typeof listener !== "function") {
      throw new TypeError("Event subscriptions require a type and listener");
    }
    const handlers = listeners.get(type) || new Set();
    handlers.add(listener);
    listeners.set(type, handlers);
    return () => {
      handlers.delete(listener);
      if (handlers.size === 0) listeners.delete(type);
    };
  }

  function emit(type, detail = undefined) {
    const handlers = [...(listeners.get(type) || [])];
    for (const listener of handlers) {
      try {
        listener(detail);
      } catch (error) {
        console.error(`[events] listener for "${type}" failed:`, error);
      }
    }
    return handlers.length;
  }

  return Object.freeze({ emit, subscribe });
}
