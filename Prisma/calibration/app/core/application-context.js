import { createLifecycle } from "./lifecycle.js";
import { CALIBRATION_CONSTANTS } from "./constants.js";

export function createApplicationContext({ api, root = document } = {}) {
  return {
    api,
    commands: {},
    constants: CALIBRATION_CONSTANTS,
    dom: { root },
    lifecycle: createLifecycle(),
    state: {
      session: {},
      navigation: {},
      logbook: {},
      images: {},
      processing: {},
      filaments: {},
      geometries: {},
      modeling: {},
      operations: {},
      ui: {},
    },
  };
}
