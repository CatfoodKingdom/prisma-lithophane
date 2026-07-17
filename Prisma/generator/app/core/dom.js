export function requiredElement(root, selector) {
  const element = root.querySelector(selector);
  if (!element) throw new Error(`Required Generator element is missing: ${selector}`);
  return element;
}

export function optionalElement(root, selector) {
  return root.querySelector(selector);
}

export function createDomRegistry(root = document) {
  return Object.freeze({
    root,
    connectionBadge: requiredElement(root, "#dataSourceBadge"),
    tabSwitch: requiredElement(root, "#tabSwitch"),
    settingsDrawer: requiredElement(root, "#settingsDrawer"),
    operationProgress: requiredElement(root, "#opProgress"),
    lightbox: requiredElement(root, "#compLightbox"),
  });
}
