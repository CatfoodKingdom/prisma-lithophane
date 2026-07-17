/**
 * Shared Generator frontend contracts. These typedefs document module boundaries
 * without adding a build step or runtime dependency.
 *
 * @typedef {Object} ApplicationState
 * @property {Record<string, unknown>} session
 * @property {Record<string, unknown>} image
 * @property {Record<string, unknown>} palette
 * @property {Record<string, unknown>} settings
 * @property {Record<string, unknown>} solve
 * @property {Record<string, unknown>} export
 * @property {Record<string, unknown>} ui
 *
 * @typedef {Object} ApplicationLifecycle
 * @property {(target: EventTarget, type: string, listener: EventListenerOrEventListenerObject, options?: AddEventListenerOptions|boolean) => void} listen
 * @property {(callback: Function, delayMs: number) => number|null} timeout
 * @property {(disposer: Function) => void} own
 * @property {() => void} dispose
 * @property {boolean} disposed
 *
 * @typedef {Object} ApplicationContext
 * @property {Record<string, Function>} api
 * @property {{STATIC_FILAMENTS: Array<Record<string, unknown>>}} data
 * @property {{pollJobUntilTerminal: Function}} services
 * @property {Record<string, Function>} commands
 * @property {ReturnType<import("./dom.js").createDomRegistry>} dom
 * @property {ApplicationLifecycle} lifecycle
 * @property {ReturnType<import("./persistence.js").createPersistence>} persistence
 * @property {ReturnType<import("./store.js").createStore>} store
 * @property {ApplicationState} state
 *
 * @typedef {Object} ApiResult
 * @property {boolean} [ok]
 * @property {string} [error]
 * @property {string} [job_id]
 * @property {string} [status]
 *
 * @typedef {Object} ControllerDependencies
 * @property {Record<string, Function>} api
 * @property {ApplicationLifecycle} lifecycle
 * @property {Record<string, Function>} commands
 */

export {};
