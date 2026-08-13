import * as api from "./api/index.js?v=2026-08-04-saving-loading-fixes-v1";
import { STATIC_FILAMENTS } from "./data.js";
import { createApplicationContext, initializeApplicationState } from "./core/application-context.js?v=2026-08-11-print-setup-v1";
import { pollJobUntilTerminal } from "./core/polling.js";
import { installFeaturesShellTheme } from "./features/shell/theme.js?v=2026-08-02-topbar-menu-switch-v1";
import { installFeaturesShellIndex } from "./features/shell/index.js?v=2026-08-11-dependent-change-review-v1";
import { installFeaturesPrintersIndex } from "./features/printers/index.js?v=2026-08-11-nozzle-identity-v1";
import { installFeaturesImageIndex } from "./features/image/index.js?v=2026-08-04-saving-loading-fixes-v1";
import { installFeaturesImageImports } from "./features/image/imports.js";
import { installFeaturesPaletteIndex } from "./features/palette/index.js?v=2026-07-30-guides-foundation-v3";
import { installFeaturesPaletteSuggestions } from "./features/palette/suggestions.js?v=2026-08-04-saving-loading-fixes-v1";
import { installFeaturesPaletteDeck } from "./features/palette/deck.js?v=2026-08-02-exact-palette-suggestions-v1";
import { installFeaturesPaletteLibrary } from "./features/palette/library.js?v=2026-08-04-saving-loading-fixes-v1";
import { installFeaturesModelLibraries } from "./features/palette/model-libraries.js?v=2026-07-30-guided-setup-v7";
import { installFeaturesSettingsController } from "./features/settings/controller.js?v=2026-08-11-print-setup-v2";
import { installFeaturesSettingsContract } from "./features/settings/contract.js?v=2026-08-11-print-setup-v1";
import { installFeaturesSettingsProfiles } from "./features/settings/profiles.js?v=2026-08-11-settings-ia-v1";
import { installFeaturesSolveController } from "./features/solve/controller.js?v=2026-07-30-generator-basics-v2";
import { installFeaturesSolveBatch } from "./features/solve/batch.js?v=2026-08-11-print-setup-v1";
import { installFeaturesSolveComparison } from "./features/solve/comparison.js";
import { installFeaturesSolveRun } from "./features/solve/run.js?v=2026-08-11-print-setup-v1";
import { installFeaturesSolveDiagnostics } from "./features/solve/diagnostics.js";
import { installFeaturesSolveLightbox } from "./features/solve/lightbox.js";
import { installFeaturesSolveRecipeViewer } from "./features/solve/recipe-viewer.js?v=2026-08-11-print-setup-v1";
import { installFeaturesSettingsModules } from "./features/settings/modules.js?v=2026-08-11-module-hierarchy-v1";
import { installFeaturesSettingsLayout } from "./features/settings/layout.js?v=2026-08-11-preprocessing-flow-v2";
import { installFeaturesGuideActionRegistry } from "./features/guides/actions/registry.js?v=2026-08-11-dependent-change-review-v1";
import { installFeaturesGuideWorkspaceActions } from "./features/guides/actions/workspace.js?v=2026-08-04-saving-loading-fixes-v1";
import { installFeaturesGuidesTargets } from "./features/guides/targets.js?v=2026-08-12-guide-companion-v4";
import { installFeaturesGuidesRegistry } from "./features/guides/registry.js?v=2026-08-12-guide-companion-v4";
import { installFeaturesGuidesOverlay } from "./features/guides/overlay.js?v=2026-08-12-guide-companion-polish-v1";
import { installFeaturesGuidesController } from "./features/guides/controller.js?v=2026-08-12-guide-companion-v4";
import { installFeaturesEventBindings } from "./features/event-bindings.js?v=2026-08-11-saved-runs-escape-v1";
import { installFeaturesApplication } from "./features/application.js?v=2026-08-04-saving-loading-fixes-v1";

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
installFeaturesSettingsContract(app);
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
installFeaturesGuideActionRegistry(app);
installFeaturesGuideWorkspaceActions(app);
installFeaturesGuidesTargets(app);
installFeaturesGuidesRegistry(app);
installFeaturesGuidesOverlay(app);
installFeaturesGuidesController(app);
installFeaturesEventBindings(app);
installFeaturesApplication(app);
initializeApplicationState(app);

app.commands.startGeneratorApp().catch((error) => {
  console.error("Generator startup failed", error);
  const badge = document.querySelector("#dataSourceBadge");
  if (badge) { badge.textContent = "startup failed"; badge.classList.remove("connected"); }
});
