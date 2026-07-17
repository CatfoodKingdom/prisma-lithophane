export function createPersistence(storage = localStorage) {
  return Object.freeze({
    read(key, fallback = null) {
      const value = storage.getItem(key);
      return value === null ? fallback : value;
    },
    readJson(key, fallback) {
      const value = storage.getItem(key);
      if (value === null) return fallback;
      try { return JSON.parse(value); } catch { return fallback; }
    },
    write(key, value) {
      storage.setItem(key, String(value));
    },
    writeJson(key, value) {
      storage.setItem(key, JSON.stringify(value));
    },
    remove(key) {
      storage.removeItem(key);
    },
  });
}
