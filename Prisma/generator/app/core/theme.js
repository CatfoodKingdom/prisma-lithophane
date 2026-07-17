export const THEME_STORAGE_KEY = "prisma_generator_theme";
export const THEME_PREFERENCES = Object.freeze(["system", "light", "dark"]);

export function normalizeThemePreference(value) {
  return THEME_PREFERENCES.includes(value) ? value : "system";
}

export function resolveTheme(preference, systemDark = false) {
  const normalized = normalizeThemePreference(preference);
  return normalized === "system" ? (systemDark ? "dark" : "light") : normalized;
}

export function applyResolvedTheme(root, preference, systemDark = false) {
  const normalized = normalizeThemePreference(preference);
  const resolved = resolveTheme(normalized, systemDark);
  root.dataset.themePreference = normalized;
  root.dataset.theme = resolved;
  root.style.colorScheme = resolved;
  return resolved;
}
