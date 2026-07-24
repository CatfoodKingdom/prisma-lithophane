/** Install core/formatting commands. */
export function installCoreFormatting(app) {
  function hexToRgbString(hex) {
    const clean = (hex || "").replace("#", "");
    if (clean.length !== 6) return "—";
    const r = parseInt(clean.slice(0, 2), 16);
    const g = parseInt(clean.slice(2, 4), 16);
    const b = parseInt(clean.slice(4, 6), 16);
    return `${r}, ${g}, ${b}`;
  }

  function normalizeHexInput(raw) {
    const clean = (raw || "").trim().replace("#", "");
    if (!/^[0-9A-Fa-f]{6}$/.test(clean)) return null;
    return "#" + clean.toUpperCase();
  }

  function sigfig(val, n = 4) {
    if (val === 0) return "0";
    return Number(val.toPrecision(n)).toString();
  }

  function textColor(hex) {
    if (!hex) return "#111";
    const clean = hex.replace("#", "");
    if (clean.length !== 6) return "#111";
    const r = parseInt(clean.slice(0, 2), 16);
    const g = parseInt(clean.slice(2, 4), 16);
    const b = parseInt(clean.slice(4, 6), 16);
    const luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    return luminance > 0.72 ? "#111" : "#fff";
  }

  function formatStepNumber(value) {
    const num = Number(value);
    if (Number.isNaN(num)) return "0.00";
    return num.toFixed(2);
  }

  function numericValue(value, fallback = 0) {
    if (typeof value === "string") {
      const trimmed = value.trim();
      if (!trimmed || trimmed === ".") return fallback;
    }
    const num = Number(value);
    return Number.isNaN(num) ? fallback : num;
  }

  Object.assign(app.commands, {
    hexToRgbString,
    normalizeHexInput,
    sigfig,
    textColor,
    formatStepNumber,
    numericValue,
  });
}
