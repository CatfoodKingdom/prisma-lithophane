/** @typedef {"session"|"image"|"palette"|"settings"|"solve"|"export"|"guides"|"ui"} StateSlice */

/**
 * Small observable store used for cross-feature state only. Feature-private
 * interaction state remains inside its controller.
 */
export function createStore(initialState) {
  const state = structuredClone(initialState);
  const listeners = new Map();

  function getState() {
    return state;
  }

  function update(slice, recipe) {
    if (!Object.hasOwn(state, slice)) throw new Error(`Unknown state slice: ${slice}`);
    recipe(state[slice]);
    for (const listener of listeners.get(slice) || []) listener(state[slice], state);
  }

  function subscribe(slice, listener) {
    const sliceListeners = listeners.get(slice) || new Set();
    sliceListeners.add(listener);
    listeners.set(slice, sliceListeners);
    return () => sliceListeners.delete(listener);
  }

  return Object.freeze({ getState, update, subscribe });
}
