import { installApiClient } from "../api/client.js";
import { installApiProfiles } from "../api/profiles.js";
import { installApiImages } from "../api/images.js";
import { installApiBackup } from "../api/backup.js";
import { installApiMaintenance } from "../api/maintenance.js";
import { installApiRecords } from "../api/records.js";
import { installApiGeometries } from "../api/geometries.js";
import { installApiModeling } from "../api/modeling.js";
import { installApiLoader } from "../api/loader.js";

export function createCalibrationApi({
  fetchImpl = globalThis.fetch?.bind(globalThis),
} = {}) {
  if (typeof fetchImpl !== "function")
    throw new Error("Calibration API requires fetch");
  const api = {
    fetchImpl,
    constants: { API_BASE: "/api" },
    state: { _apiLoadingState: "idle", _apiErrorMessage: "" },
  };
  installApiClient(api);
  installApiProfiles(api);
  installApiImages(api);
  installApiBackup(api);
  installApiMaintenance(api);
  installApiRecords(api);
  installApiGeometries(api);
  installApiModeling(api);
  installApiLoader(api);
  return api;
}
