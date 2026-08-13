import { chapter, completion, freezeGuide, step } from "../../core/schema.js";

const HELP_POINTER_STEPS = Object.freeze([
  step({
    id: "help-and-guides",
    title: "Help is always available",
    body: "You can reopen First-Time Setup and launch other available guides here whenever you need them.",
    target: "topbar.help-guides",
    completionMode: completion("interaction"),
    allowSkip: false,
  }),
]);

export const GUIDED_SETUP_HELP_POINTER = freezeGuide({
  id: "guided-setup-help-pointer",
  kind: "help-pointer",
  workspace_policy: "non-destructive",
  version: 1,
  title: "Find guides later",
  summary: "Point to Help & Guides after First-Time Setup is declined.",
  restore_presentation: false,
  internal: true,
  chapters: Object.freeze([chapter("help", "Help", HELP_POINTER_STEPS)]),
  steps: HELP_POINTER_STEPS,
});

const PREVIEW_STEPS = Object.freeze([
  step({
    id: "workflow-tabs",
    title: "Prisma's workflow",
    body: "These numbered tabs follow the normal path from choosing an image through exporting a finished lithophane.",
    target: "workflow.tabs",
    placements: ["bottom", "right", "top", "left"],
  }),
  step({
    id: "settings-button",
    title: "Open Settings",
    body: "Select Settings. This guide continues after Prisma confirms that the drawer opened.",
    target: "topbar.settings",
    placements: ["bottom", "left", "top", "right"],
    completionMode: completion("event", {
      event: "settings.opened",
      predicate_id: "settings.drawer-open",
    }),
  }),
  step({
    id: "white-point-rescale",
    title: "Settings can be identified in place",
    body: "This is White-point rescale. The guide frames the existing row without changing the setting or layout.",
    target: "settings.white-point-rescale",
    placements: ["left", "bottom", "top", "right"],
  }),
  step({
    id: "image-library",
    title: "Guides can follow the workflow",
    body: "Prisma can reveal another part of the interface and keep the explanation attached across different window sizes and zoom levels.",
    target: "image.library",
  }),
]);

export const INTERFACE_PREVIEW_GUIDE = freezeGuide({
  id: "interface-preview",
  kind: "preview",
  workspace_policy: "preview",
  version: 1,
  title: "Explore the Interface",
  summary: "Preview how Prisma can identify controls without changing them.",
  restore_presentation: true,
  catalog: Object.freeze({ group: "Preview", order: 10, badge: "Preview" }),
  chapters: Object.freeze([chapter("interface", "Interface", PREVIEW_STEPS)]),
  steps: PREVIEW_STEPS,
});
