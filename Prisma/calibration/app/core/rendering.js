/**
 * Install the shared rendering boundary.
 *
 * Feature templates must escape values according to their destination. The
 * legacy aliases remain deliberately narrow because existing templates rely on
 * their exact output; new code should prefer escapeHtml/escapeAttribute.
 */
export function installCoreRendering(app) {
  function escapeHtml(value) {
    return String(value ?? "").replace(
      /[&<>"']/g,
      (character) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[character],
    );
  }

  function escapeAttribute(value) {
    return escapeHtml(value);
  }

  function legacyEscapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function legacyEscapeAttribute(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;");
  }

  /** Set markup assembled exclusively from trusted literals and escaped data. */
  function setTrustedHtml(element, trustedMarkup) {
    if (!element) return;
    element.innerHTML = String(trustedMarkup ?? "");
  }

  Object.assign(app.commands, {
    escapeHtml,
    escapeAttribute,
    _escHtml: legacyEscapeHtml,
    _escAttr: legacyEscapeAttribute,
    setTrustedHtml,
  });
}
