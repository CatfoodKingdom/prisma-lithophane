import { chapter, completion, freezeGuide, step } from "../../core/schema.js?v=2026-08-04-saving-loading-fixes-v1";

const manual = (predicate_id = null) => completion("manual", predicate_id ? { predicate_id } : {});
const event = (name, predicate_id, auto_advance = true) => completion("event", {
  event: name,
  predicate_id,
  auto_advance,
});
const PRESENTATION_REVEALS = Object.freeze({
  "palette-saving-introduction": "saving.palette-page",
  "solved-run-saving-introduction": "saving.preview-page",
  "explain-temp-run-profile": "saving.settings-drawer",
});
const TARGET_OVERRIDES = Object.freeze({
  "delete-named-settings-profile": "saving.settings-profiles-reopen",
  "delete-saved-run-record": "saving.saved-runs-reopen",
});
const make = (id, title, body, options = {}) => {
  const target = TARGET_OVERRIDES[id] || options.target || null;
  return step({
    id,
    title,
    body,
    target,
    reveal: options.reveal ?? PRESENTATION_REVEALS[id] ?? target,
    overlayMode: target ? "spotlight" : "full-scrim",
    placementGroup: options.group || null,
    viewportAnchor: options.viewportAnchor || null,
    completionMode: options.completion || manual(),
    participatingSurfaces: options.surfaces || [],
    allowPrevious: options.allowPrevious ?? true,
    allowSkip: false,
    nextLabel: options.nextLabel || null,
  });
};

const steps = Object.freeze([
  make("saving-loading-introduction", "Saving different kinds of work", "**Saving and loading in Prisma**\n\nPrisma does not use one project file for everything in the current workspace. Instead, it preserves work at the level you may want to reuse: Palettes, Settings Profiles, Solved Runs, and generated Exports.\n\nEach kind of save contains different information and has different reuse behavior. In this guide, you will save and retrieve a Palette, Settings Profile, and Solved Run; learn why Exports are different; then identify the working changes that remain temporary.", { allowPrevious: false, nextLabel: "Begin" }),
  make("prepare-saving-loading-workspace", "Review the prepared workspace", "**Your tutorial workspace is ready**\n\nPrisma has selected **{{tutorialImage}}**, loaded the built-in **Basic** Settings Profile, and added **{{paletteToSave}}** and **{{supportPalette}}** to the Palette Deck. **{{paletteToSave}}** is active.\n\nThe tutorial workspace will be cleared when this guide ends, and Prisma will restore the configuration choices it recorded before launch. The Saved Run you create during the guide will remain available until you delete it.", { completion: manual("saving.prepared") }),
  make("palette-saving-introduction", "Saved Palettes and the Palette Deck", "**Palettes: save a reusable combination**\n\nThere are many reasons you might want to save a Palette to use later. Reusing a Saved Palette can help ensure visual consistency when you are printing a series of related images. You can save a Palette that is particularly effective at reproducing certain color families. You might just want to stop for the day and resume your work with that Palette at a later date.\n\nAny Palette in the Palette Deck can be saved for use in future sessions. Saving a Palette makes it available through **Load**. Loading a Saved Palette adds a copy of it to the Palette Deck.\n\nThe copy of a Saved Palette that gets added to the Deck is distinct from the save itself. Removing a Palette from the Deck does not delete a corresponding Saved Palette, and deleting the Saved Palette does not remove the Palette from the Deck."),
  make("save-palette", "Save the prepared Palette", "Saving a Palette also allows you to give it a custom name. This can help you find and load that particular combination of Filaments later or help you remember its intended use.\n\nThe **Save** action can be accessed by clicking the **⋯** menu on the Palette's card.\n\nOpen the **⋯** menu on **{{paletteToSave}}**, select **Save Palette**, enter **{{savedPaletteName}}**, and select **Save**.", { target: "saving.palette-card", group: "palette", viewportAnchor: "center-right", surfaces: ["#deckCardMenu", "#paletteSaveModal"], completion: event("palette.saved.created", "saving.palette-saved") }),
  make("remove-saved-palette-from-deck", "Remove the Palette from the Deck", "The **×** on a Palette card removes the Palette from the Deck. The first selection changes **×** to **!**; select **!** promptly to confirm. This removes the Palette from the Deck but does not delete the Palette saved under **{{savedPaletteName}}**, which remains available through **Load**.\n\nOn **{{savedPaletteName}}**, select **×**, then select **!** to remove the Palette from the Deck.", { target: "saving.palette-card", group: "palette", viewportAnchor: "center-right", surfaces: ["#deckCardMenu"], completion: event("palette.deck.updated", "saving.palette-removed") }),
  make("load-saved-palette", "Load the Saved Palette", "**Load** in the Palette Deck header lists Palettes saved for later sessions. Loading one adds the Palette to the Deck and makes it active.\n\nSelect **Load**, then select **{{savedPaletteName}}**.", { target: "saving.palette-load", group: "palette", completion: event("palette.saved.loaded", "saving.palette-loaded") }),
  make("delete-saved-palette-record", "Delete the Saved Palette", "Deleting a Palette from the **Load** list means it will no longer be available to load later. It does not remove the Palette already in the Palette Deck. Once its saved source is deleted, the Deck copy is no longer marked **Saved**, but its Filaments and active state do not change.\n\nSelect **Load**. On **{{savedPaletteName}}**, select the **×**, then select **Del?** to confirm. Watch the Palette remain in the Deck.", { target: "saving.palette-load", group: "palette", completion: event("palette.saved.deleted", "saving.palette-save-deleted") }),
  make("settings-saving-introduction", "Saved Settings Profiles and unsaved changes", "**Settings Profiles: save a reusable configuration**\n\nA Settings Profile stores the complete collection of Solve settings. After a Settings Profile is loaded, changing a setting marks it **modified**. Those changes remain unsaved until you explicitly save them.\n\nThe built-in **Basic** Settings Profile is protected. You can change its Settings, but you must save those changes under a new name."),
  make("modify-basic-profile", "Modify Basic", "The Settings Profile bar currently shows **Basic** with no modified badge. **Basic** uses **Max Total Thickness: 3.0 mm**.\n\nChange **Max Total Thickness** to **2.6 mm**. The **modified** badge shows that the current Settings now differ from the saved Basic Settings Profile.", { target: "saving.settings-thickness", group: "settings", completion: event("config.synced", "saving.settings-2.6-modified") }),
  make("save-named-settings-profile", "Save the changed Settings under a new name", "**Basic** cannot be overwritten. When you select **Save**, Prisma offers to save the changed Settings as a new Settings Profile instead.\n\nSelect **Save**. In **Save Settings Profile**, select **Save As**. Enter **{{savedProfileName}}** and select **Save**.", { target: "saving.settings-save", group: "settings", viewportAnchor: "center-left", surfaces: ["#appDialog", "#settingsProfileSaveModal"], completion: event("settings.profile.created", "saving.settings-profile-saved") }),
  make("modify-named-settings-profile", "Change the new Settings Profile without saving", "`{{savedProfileName}}` is now saved with **Max Total Thickness: 2.6 mm**.\n\nChange **Max Total Thickness** back to **3.0 mm**, but do not save. The **modified** badge shows that the current Settings differ from the saved Settings Profile.", { target: "saving.settings-thickness", group: "settings", completion: event("config.synced", "saving.settings-3.0-modified") }),
  make("open-settings-profiles-with-modified-draft", "See what Reload Saved Version will restore", "The Profiles window lists Saved Settings Profiles. When the loaded Settings Profile has unsaved changes, selecting that same Settings Profile offers **Reload Saved Version**. Reloading would discard the current **3.0 mm** value and restore the **2.6 mm** value you saved earlier.\n\nWhen you load a different Settings Profile, Prisma first asks whether to save or discard the unsaved changes, or cancel the load.\n\nSelect **Profiles…** to view `{{savedProfileName}}` and its saved version.", { target: "saving.settings-profiles", group: "settings", surfaces: ["#settingsProfileModal"], completion: event("settings.profile-browser.opened", "saving.settings-browser-open", false) }),
  make("load-basic-discard-settings-draft", "Load Basic and discard the unsaved changes", "Unsaved Settings changes do not survive a Prisma restart. They are also not included when you load a saved Settings Profile unless you save them first.\n\nSelect **Basic**, then select **Load**. When Prisma asks what to do with the unsaved changes in `{{savedProfileName}}`, choose **Discard**.", { target: "saving.settings-profiles", group: "settings", surfaces: ["#settingsProfileModal", "#appDialog"], completion: event("settings.profile.loaded", "saving.basic-restored") }),
  make("delete-named-settings-profile", "Startup Settings Profiles and deletion", "The star action in **Settings Profiles** chooses which Settings Profile Prisma loads at startup. Changing the startup Settings Profile does not load it now, and it does not save any unsaved changes. A **TEMP** Settings Profile loaded from a Solved Run cannot be selected as the startup Settings Profile.\n\nThe built-in **Basic** Settings Profile cannot be deleted. Named Settings Profiles can be deleted after another Settings Profile is loaded.\n\nSelect `{{savedProfileName}}`, select its **×** twice, then select **Delete** in the confirmation dialog. Do not change the startup Settings Profile during this guide.", { target: "saving.settings-profiles", group: "settings", surfaces: ["#settingsProfileModal", "#appDialog"], completion: event("settings.profile.deleted", "saving.settings-profile-deleted") }),
  make("solved-run-saving-introduction", "A Saved Run preserves a completed result", "**Solved Runs: preserve a complete result**\n\nA Saved Run preserves the source Image, its framing and appearance, the Palette, the Settings, and the result from one completed Solve. Loading it returns the Solved Run to Solve History and its result to Preview, where it can be inspected or exported without repeating the Solve.\n\nSolve History lasts only for the current session. Clearing its cards does not delete a Solved Run that was explicitly saved."),
  make("solve-for-saving-loading", "Complete a Solve", "This Solve will use the active **{{savedPaletteName}}** and **Basic** Settings. It may take a little while, and this step advances only after a completed result is available.\n\nSelect **Solve**.", { target: "saving.solve", group: "runs", surfaces: ["#opProgress"], completion: event("solve.completed", "saving.solve-complete") }),
  make("save-solved-run", "Save the completed Solved Run", "Saving the completed Solved Run preserves its result and the Image, Palette, and Settings it used so you can return to it after a restart. Prisma uses names such as “Run 7” for Solved Runs in Solve History, so give this Saved Run a more descriptive name.\n\nSelect **Save** on the completed Solved Run card, enter **{{savedRunName}}**, and select **Save** in the dialog.", { target: "saving.run-card", group: "runs", surfaces: ["#appDialog"], completion: event("solve.saved-run.created", "saving.run-saved") }),
  make("clear-solve-history-for-load", "Clear the visible Solve History", "**Clear** removes cards from the current Solve History display. It does not delete the Saved Run you just created.\n\nSelect **Clear**, then select **Clear?**. Watch the Preview card disappear.", { target: "saving.history-clear", group: "runs", completion: event("solve.history.cleared", "saving.history-cleared") }),
  make("clear-palette-deck-for-run-load", "Clear the Palette Deck", "Loading a complete Saved Run also adds its Palette to the Palette Deck. Clearing the Palette Deck now will make that easy to see.\n\nIn the Palette Deck header, select **Clear**, then select **Clear?**.", { target: "saving.palette-clear", group: "runs", completion: event("palette.deck.cleared", "saving.deck-cleared") }),
  make("load-complete-saved-run", "Load the complete Saved Run", "**Load** opens Saved Runs. Rows marked **Saved** are explicit durable saves. The window may also contain **Autosave** rows. Autosaves are temporary, cache-managed copies: they may survive a Prisma restart, but Prisma can evict older Autosaves as newer ones are created, and **Clear Temp Files** removes them. Use **Save** when you want to preserve a Run intentionally.\n\nSelect **{{savedRunName}}**, confirm that it is marked **Saved**, and select **Load**. The Solved Run will return to Solve History, its result will appear in Preview, and its Palette will return to the Palette Deck without running another Solve.", { target: "saving.saved-runs", group: "runs", surfaces: ["#savedRunsModal"], completion: event("solve.saved-run.loaded", "saving.run-loaded") }),
  make("open-run-settings", "Open the Solved Run's captured Settings", "Every Solved Run keeps the Settings that were captured when its Solve began. Later changes in the Settings drawer do not change the Settings recorded by the Solved Run.\n\nSelect **Settings** on the `{{savedRunName}}` card.", { target: "saving.run-card", group: "runs", completion: event("solve.run-settings.opened", "saving.run-settings-open") }),
  make("use-run-settings", "Use the captured Settings", "**Use These Settings** loads this Solved Run's captured Settings as a **TEMP** Settings Profile. It does not alter the Saved Run or overwrite **Basic**.\n\nThis action also works on an unsaved Solved Run while it remains in Solve History during the current session.\n\nSelect **Use These Settings**.", { target: "saving.run-settings", group: "runs", completion: event("settings.temp-profile.loaded", "saving.temp-loaded") }),
  make("explain-temp-run-profile", "TEMP Settings Profiles and Settings-only loading", "The Settings Profile bar now identifies `{{savedRunName}}` as **TEMP**. A TEMP Settings Profile can be changed and used for another Solve, but it exists only for this session. It cannot be selected as the startup Settings Profile, and saving it requires **Save As** to create a normal named Settings Profile.\n\nFor an explicitly Saved Run, you do not have to load the complete result first. Open **Load**, select the Saved Run, and choose **Use Settings** to create the same kind of TEMP Settings Profile without loading the rest of the Saved Run."),
  make("delete-saved-run-record", "Delete the Saved Run", "The Saved Run and the Solved Run card loaded from it are distinct. Deleting the Saved Run removes the durable copy from **Saved Runs**, but it does not remove the loaded card from Solve History or unload the TEMP Settings Profile you are using now.\n\nOpen **Load**, select **{{savedRunName}}**, select **Delete**, then select **Confirm?**.", { target: "saving.saved-runs", group: "runs", surfaces: ["#savedRunsModal"], completion: event("solve.saved-run.deleted", "saving.run-save-deleted") }),
  make("export-saving-introduction", "Exporting is a final save operation", "**Exports: final files rather than reusable workspace state**\n\n**Generate Print Files** is itself a save operation. It writes the selected Solved Run's final 3MF or STL files, instructions, and report into Prisma's Exports folder.\n\nThose files are the product of a Solve, not an editable input to Prisma. There is no Export **Load** action. To make different files, return to a Solved Run and generate another Export.", { target: "saving.export", group: "export" }),
  make("explain-export-file-ownership", "Final files are managed outside Prisma", "Each successful Export is written into its own Output folder. After an Export exists, **Open Folder** takes you directly to those files.\n\nBecause Exports are ordinary final files, organize, rename, copy, or delete them with your system's file explorer. Prisma does not load them back into the workflow or manage them like Palettes, Settings Profiles, or Saved Runs.", { target: "saving.export", group: "export" }),
  make("explain-export-downloads", "Use downloads only when you need another copy", "The links under **Generated Files** download individual files through the browser. **Download .zip** packages the selected Export into one additional copy.\n\nThese downloads are useful for archival, portability, or moving files to another computer. For ordinary printing on this computer, use **Open Folder** and take the already-saved files from Prisma's Output folder instead of downloading duplicates.", { target: "saving.export", group: "export" }),
  make("persistence-boundaries-introduction", "Prisma does not save one editable project", "**Choose what you will need later**\n\nPrisma saves Palettes, Settings Profiles, and Solved Runs separately, and it produces Exports as final files. There is no command that saves all of your current choices together as one editable project.\n\nA Saved Run comes closest to a project file because it preserves one completed Solve. It contains what that Solve used, not every experiment or interface choice made before or after it."),
  make("unsaved-working-state-boundaries", "Working state that cannot be saved independently", "Some choices do not have their own **Save** action:\n\n- An imported source Image remains in the Image Library, but its current selection, framing, crop, physical canvas, border, and appearance are not a separate preset. A Saved Run preserves the Image setup used for its Solve.\n- You cannot save which Palette is active or save the entire contents and order of the Palette Deck together. A Suggested Palette or Manual Palette must be added to the Deck before you can save that Palette by name.\n- Changes to Settings remain unsaved until you save the complete Settings Profile.\n- Solve History, including which Solved Run cards are selected for comparison, is not saved as a whole. Save an individual Solved Run when its result and captured inputs matter.\n- Export choices are not reusable presets. Once files are generated, the final Export itself is already saved.\n\nBefore ending a session, decide whether you need a reusable Palette, a Settings Profile, a complete Saved Run, final Export files, or some combination of them."),
  make("saving-loading-complete", "Saving & Loading complete", "**Saving & Loading complete**\n\nYou saved and loaded a Palette, created and removed a named Settings Profile, saved, restored, and deleted a complete Solved Run, and recovered a Solved Run's Settings as a TEMP Settings Profile. You also learned why Exports are final files that cannot be loaded back into Prisma.\n\nThe Palette, Settings Profile, and Saved Run created during this guide have all been removed. Prisma will now clear the tutorial workspace and restore the configuration choices it recorded before the guide began. Project work cleared with your consent at launch is not recreated."),
]);

const FILAMENTS = Object.freeze([
  "bambu-tough-white",
  "bambu-basic-cyan", "bambu-basic-magenta", "bambu-basic-yellow",
  "bambu-basic-red", "bambu-basic-blue", "bambu-basic-orange",
]);

export const SAVING_AND_LOADING_GUIDE = freezeGuide({
  id: "saving-and-loading",
  canonical_guide_id: "saving-and-loading",
  route_id: "full",
  kind: "teaching",
  workspace_policy: "basic-teaching",
  version: 1,
  title: "Saving & Loading",
  summary: "Save and restore Palettes, Settings Profiles, and completed Solved Runs.",
  catalog: Object.freeze({ group: "Save, Reuse, and Export", order: 10 }),
  restore_presentation: false,
  baseline: Object.freeze({ ghost_printer: true, guide_assets: Object.freeze(["bubba-blanket"]) }),
  preflight_actions: Object.freeze([
    Object.freeze({ action: "settings.require_basic", input: Object.freeze({ values: Object.freeze({ t_max: 3, layer_height: 0.08, d_wb: 0.2, d_wc_min: 0.16, base_filament: "bambu-tough-white", cap_filament: "__same__", luminance_mode: "standard" }) }), result_key: "basicProfile" }),
    Object.freeze({ action: "filaments.require", input: Object.freeze({ filament_ids: FILAMENTS }), result_key: "requiredFilaments" }),
    Object.freeze({ action: "guide.allocate_names", input: Object.freeze({ palette: "Saving & Loading Palette", profile: "Saving & Loading Profile", run: "Saving & Loading Run" }), result_key: "names" }),
  ]),
  preparation_actions: Object.freeze([
    Object.freeze({ action: "printer.mount_ghost", result_key: "tutorialPrinterProfile" }),
    Object.freeze({ action: "printer.select", input: Object.freeze({ printer_id: "tutorial-printer" }) }),
    Object.freeze({ action: "printer.select_nozzle", input: Object.freeze({ nozzle_mm: 0.2 }) }),
    Object.freeze({ action: "settings.load_basic", input: Object.freeze({ load_as: "system" }) }),
    Object.freeze({ action: "filaments.select", input: Object.freeze({ filament_ids: FILAMENTS }) }),
    Object.freeze({ action: "palette.deck.replace", input: Object.freeze({ palettes: Object.freeze([
      Object.freeze({ name: "Tutorial Primary Palette", filament_ids: Object.freeze(["bambu-basic-cyan", "bambu-basic-magenta", "bambu-basic-yellow"]) }),
      Object.freeze({ name: "Tutorial Accent Palette", filament_ids: Object.freeze(["bambu-basic-red", "bambu-basic-blue", "bambu-basic-orange"]) }),
    ]) }), result_key: "tutorialPalettes" }),
    Object.freeze({ action: "palette.deck.activate", input: Object.freeze({ index: 0 }) }),
    Object.freeze({ action: "image.mount_guide_asset", input: Object.freeze({ asset_id: "bubba-blanket" }), result_key: "tutorialImage" }),
    Object.freeze({ action: "image.select", input: Object.freeze({ asset_id: "bubba-blanket", width_mm: 30, height_mm: 40 }) }),
  ]),
  text_substitutions: Object.freeze({
    "{{paletteToSave}}": "tutorialPalettes.0.name",
    "{{supportPalette}}": "tutorialPalettes.1.name",
    "{{savedPaletteName}}": "names.savedPaletteName",
    "{{savedProfileName}}": "names.savedProfileName",
    "{{savedRunName}}": "names.savedRunName",
  }),
  durable_mutation_policy: Object.freeze({
    default: "deny",
    steps: Object.freeze({
      "save-palette": Object.freeze([Object.freeze({
        operation: "palette.saved.create",
        match: Object.freeze({
          deck_card_id: Object.freeze({ context: "tutorialPalettes.0.id" }),
          palette_signature: Object.freeze({ context: "tutorialPalettes.0.filament_ids" }),
          name: Object.freeze({ context: "names.savedPaletteName" }),
        }),
      })]),
      "delete-saved-palette-record": Object.freeze([Object.freeze({
        operation: "palette.saved.delete",
        match: Object.freeze({ saved_record_id: Object.freeze({ context: "savedPaletteId" }) }),
      })]),
      "save-named-settings-profile": Object.freeze([Object.freeze({
        operation: "settings.profile.create",
        match: Object.freeze({
          name: Object.freeze({ context: "names.savedProfileName" }),
          source_profile_id: Object.freeze({ context: "basicProfile.id" }),
          t_max: Object.freeze({ literal: 2.6 }),
        }),
      })]),
      "delete-named-settings-profile": Object.freeze([Object.freeze({
        operation: "settings.profile.delete",
        match: Object.freeze({ profile_id: Object.freeze({ context: "savedProfileId" }) }),
      })]),
      "save-solved-run": Object.freeze([Object.freeze({
        operation: "solve.saved-run.create",
        match: Object.freeze({
          run_card_id: Object.freeze({ context: "guideRunId" }),
          label: Object.freeze({ context: "names.savedRunName" }),
        }),
      })]),
      "delete-saved-run-record": Object.freeze([Object.freeze({
        operation: "solve.saved-run.delete",
        match: Object.freeze({ save_id: Object.freeze({ context: "savedRunId" }) }),
      })]),
    }),
  }),
  chapters: Object.freeze([
    chapter("introduction", "Introduction and preparation", steps.slice(0, 2)),
    chapter("palettes", "Palettes", steps.slice(2, 7)),
    chapter("settings", "Settings Profiles", steps.slice(7, 14)),
    chapter("runs", "Solved Runs", steps.slice(14, 24)),
    chapter("exports", "Exports", steps.slice(24, 27)),
    chapter("boundaries", "Persistence boundaries and completion", steps.slice(27, 30)),
  ]),
  steps,
});
