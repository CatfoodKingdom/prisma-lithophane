import { chapter, completion, freezeGuide, step } from "../../core/schema.js";

const STEPS = Object.freeze([
  step({
    id: "printer-open",
    title: "Configure your printer",
    body: "Let's begin by adding details about your 3D printer to Prisma Generator.\n\nSelect **Configure** to open the Printer Configuration window.",
    target: "sidebar.printer",
    viewportAnchor: "center",
    completionMode: completion("event", {
      event: "printer-config.opened",
      predicate_id: "printer-config.open",
      accept_preexisting: true,
    }),
  }),
  step({
    id: "printer-configuration",
    title: "Review printer setup",
    body: "Please modify the current profile to match your 3D printer’s name, build dimensions, and AMS configuration. Printer Configuration saves your changes automatically.\n\nIf you have multiple 3D printers, you can create a new profile by selecting **+ New** and filling in the necessary details.\n\nWhen you finish, select the **✕** button in the upper-right corner to close this window.",
    target: "printer.configuration",
    placements: ["left", "bottom", "top", "right"],
    completionMode: completion("event", {
      event: "printer-config.closed",
      predicate_id: "printer-config.closed",
    }),
  }),
  step({
    id: "active-extrusion-width",
    title: "Changing the Extrusion Width",
    body: "Select the Extrusion Width you will use in your slicer. You can choose a saved value or add any value supported by the active nozzle. Prisma uses it to keep horizontal features printable and to determine the available Solve Pitch values.",
    target: "sidebar.active-extrusion-width",
    placementGroup: "setup-sidebar",
  }),
  step({
    id: "model-library",
    title: "Model Libraries",
    body: "A Model Library supplies Prisma Generator with information it uses to predict how a lithophane will look when different filaments are combined. Prisma comes pre-installed with a Model Library that includes many common filaments.\n\nYou can use **Manage** to install a new library or to change the active library. You can create your own custom Model Library using Prisma Calibration if you need support for filaments not included in the pre-installed library. Changing the active library restarts Prisma Generator, so do not change it until you finish this guide.",
    target: "sidebar.model-library",
    placementGroup: "setup-sidebar",
  }),
  step({
    id: "active-filaments",
    title: "Active Filaments",
    body: "Active Filaments lets you make persistent choices about which filaments from the active Model Library will be available for use by Prisma Generator. You can activate or deactivate filaments by selecting **Manage**. These selections persist across sessions.\n\nDeactivating a filament prevents Prisma Generator from using or displaying it elsewhere. This can be useful if you do not own a filament or if a filament has been discontinued by its manufacturer.",
    target: "sidebar.active-filaments",
    placementGroup: "setup-sidebar",
  }),
  step({
    id: "theme",
    title: "Choose a theme",
    body: "This button allows you to change the color theme of Prisma Generator’s interface. Please change it to the option you prefer.",
    target: "topbar.theme",
    placementGroup: "setup-utilities",
    placements: ["left", "top", "right", "bottom"],
  }),
  step({
    id: "temporary-files",
    title: "Temporary files",
    body: "Prisma automatically clears temporary data when a new session starts. **Clear Temp Files** allows you to manually clear data during a session.",
    target: "topbar.clear-temp",
    placementGroup: "setup-utilities",
    placements: ["bottom", "left", "top", "right"],
  }),
  step({
    id: "help-and-guides",
    title: "Help & Guides",
    body: "Many useful in-app guides are available from **Help & Guides**.",
    target: "topbar.help-guides",
    placementGroup: "setup-utilities",
    placements: ["bottom", "left", "top", "right"],
  }),
  step({
    id: "complete",
    title: "Ready to create",
    body: "First-Time Setup is complete, and Prisma Generator is ready to use. Happy printing!",
    target: null,
    placements: [],
    allowSkip: false,
    followup: {
      guide_id: "prisma-generator-basics",
      text: "If you are new to Prisma Generator, you may want to continue with a guided introduction to its features and workflow.",
      label: "Start Prisma Generator Basics",
    },
  }),
]);

export const GUIDED_SETUP_GUIDE = freezeGuide({
  id: "guided-setup",
  kind: "setup",
  workspace_policy: "non-destructive",
  version: 1,
  title: "First-Time Setup",
  summary: "Configure Prisma Generator for your printer and learn its essential utility controls.",
  restore_presentation: false,
  catalog: Object.freeze({ group: "Getting Started", order: 10 }),
  chapters: Object.freeze([chapter("setup", "Setup", STEPS)]),
  steps: STEPS,
});
