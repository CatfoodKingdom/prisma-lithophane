import * as api from "./api/index.js";
import { STATIC_FILAMENTS } from "./data.js";
import { createApplicationContext, initializeApplicationState } from "./core/application-context.js";
import { pollJobUntilTerminal } from "./core/polling.js";
import { installFeaturesShellTheme } from "./features/shell/theme.js";
import { installFeaturesShellIndex } from "./features/shell/index.js";
import { installFeaturesPrintersIndex } from "./features/printers/index.js";
import { installFeaturesImageIndex } from "./features/image/index.js";
import { installFeaturesPaletteIndex } from "./features/palette/index.js";
import { installFeaturesPaletteSuggestions } from "./features/palette/suggestions.js";
import { installFeaturesPaletteDeck } from "./features/palette/deck.js";
import { installFeaturesPaletteLibrary } from "./features/palette/library.js";
import { installFeaturesModelLibraries } from "./features/palette/model-libraries.js";
import { installFeaturesSettingsController } from "./features/settings/controller.js";
import { installFeaturesSettingsProfiles } from "./features/settings/profiles.js";
import { installFeaturesSolveController } from "./features/solve/controller.js";
import { installFeaturesSolveComparison } from "./features/solve/comparison.js";
import { installFeaturesSolveRun } from "./features/solve/run.js";
import { installFeaturesSolveDiagnostics } from "./features/solve/diagnostics.js";
import { installFeaturesSolveLightbox } from "./features/solve/lightbox.js";
import { installFeaturesSolveRecipeViewer } from "./features/solve/recipe-viewer.js";
import { installFeaturesSettingsModules } from "./features/settings/modules.js";
import { installFeaturesSettingsLayout } from "./features/settings/layout.js";
import { installFeaturesEventBindings } from "./features/event-bindings.js";
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
installFeaturesPaletteIndex(app);
installFeaturesPaletteSuggestions(app);
installFeaturesPaletteDeck(app);
installFeaturesPaletteLibrary(app);
installFeaturesModelLibraries(app);
installFeaturesSettingsController(app);
installFeaturesSettingsProfiles(app);
installFeaturesSolveController(app);
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
