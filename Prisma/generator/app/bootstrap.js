import * as api from "./api/index.js";
import { STATIC_FILAMENTS } from "./data.js";
import { createApplicationContext, initializeApplicationState } from "./core/application-context.js?v=2026-07-30-settings-flow-v2";
import { pollJobUntilTerminal } from "./core/polling.js";
import { installFeaturesShellTheme } from "./features/shell/theme.js";
import { installFeaturesShellIndex } from "./features/shell/index.js?v=2026-07-28-solve-pitch-remediation-v5";
import { installFeaturesPrintersIndex } from "./features/printers/index.js";
import { installFeaturesImageIndex } from "./features/image/index.js";
import { installFeaturesImageImports } from "./features/image/imports.js";
import { installFeaturesPaletteIndex } from "./features/palette/index.js";
import { installFeaturesPaletteSuggestions } from "./features/palette/suggestions.js?v=2026-07-30-cap-summary-removal-v1";
import { installFeaturesPaletteDeck } from "./features/palette/deck.js";
import { installFeaturesPaletteLibrary } from "./features/palette/library.js";
import { installFeaturesModelLibraries } from "./features/palette/model-libraries.js";
import { installFeaturesSettingsController } from "./features/settings/controller.js?v=2026-07-30-cap-summary-removal-v1";
import { installFeaturesSettingsProfiles } from "./features/settings/profiles.js?v=2026-07-28-solve-pitch-remediation-v5";
import { installFeaturesSolveController } from "./features/solve/controller.js?v=2026-07-30-settings-flow-v2";
import { installFeaturesSolveBatch } from "./features/solve/batch.js?v=2026-07-28-solve-pitch-remediation-v5";
import { installFeaturesSolveComparison } from "./features/solve/comparison.js";
import { installFeaturesSolveRun } from "./features/solve/run.js?v=2026-07-30-settings-flow-v2";
import { installFeaturesSolveDiagnostics } from "./features/solve/diagnostics.js";
import { installFeaturesSolveLightbox } from "./features/solve/lightbox.js";
import { installFeaturesSolveRecipeViewer } from "./features/solve/recipe-viewer.js?v=2026-07-28-solve-pitch-remediation-v5";
import { installFeaturesSettingsModules } from "./features/settings/modules.js?v=2026-07-30-cap-summary-removal-v1";
import { installFeaturesSettingsLayout } from "./features/settings/layout.js?v=2026-07-30-settings-flow-v3";
import { installFeaturesEventBindings } from "./features/event-bindings.js?v=2026-07-30-cap-summary-removal-v1";
import { installFeaturesApplication } from "./features/application.js";

const app = createApplicationContext({
  api,
  data: { STATIC_FILAMENTS },
  services: { pollJobUntilTerminal },
});
installFeaturesShellTheme(app);
installFeaturesShellIndex(app);
installFeaturesPrintersIndex(app);
installFeaturesImageIndex(app);
installFeaturesImageImports(app);
installFeaturesPaletteIndex(app);
installFeaturesPaletteSuggestions(app);
installFeaturesPaletteDeck(app);
installFeaturesPaletteLibrary(app);
installFeaturesModelLibraries(app);
installFeaturesSettingsController(app);
installFeaturesSettingsProfiles(app);
installFeaturesSolveController(app);
installFeaturesSolveBatch(app);
installFeaturesSolveComparison(app);
installFeaturesSolveRun(app);
installFeaturesSolveDiagnostics(app);
installFeaturesSolveLightbox(app);
installFeaturesSolveRecipeViewer(app);
installFeaturesSettingsModules(app);
installFeaturesSettingsLayout(app);
installFeaturesEventBindings(app);
installFeaturesApplication(app);
initializeApplicationState(app);

app.commands.startGeneratorApp().catch((error) => {
  console.error("Generator startup failed", error);
  const badge = document.querySelector("#dataSourceBadge");
  if (badge) { badge.textContent = "startup failed"; badge.classList.remove("connected"); }
});
