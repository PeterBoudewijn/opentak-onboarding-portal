const AUTH_KEYS = ['access_token', 'refresh_token', 'user'];

function safeGet(storage, key) {
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

function safeSet(storage, key, value) {
  try {
    if (value === null || value === undefined) {
      storage.removeItem(key);
    } else {
      storage.setItem(key, value);
    }
  } catch {
    // Storage can be unavailable in private/restricted browser contexts.
  }
}

function safeRemove(storage, key) {
  try {
    storage.removeItem(key);
  } catch {
    // Ignore storage cleanup failures.
  }
}

function migrateLegacyLocalStorage() {
  // Older portal versions persisted JWTs in localStorage. Move them into
  // per-tab sessionStorage once, then remove the persistent copies.
  for (const key of AUTH_KEYS) {
    const current = safeGet(sessionStorage, key);
    const legacy = safeGet(localStorage, key);
    if (!current && legacy) {
      safeSet(sessionStorage, key, legacy);
    }
    if (legacy) {
      safeRemove(localStorage, key);
    }
  }
}

migrateLegacyLocalStorage();

export const authStorage = {
  getAccessToken() {
    migrateLegacyLocalStorage();
    return safeGet(sessionStorage, 'access_token');
  },

  getRefreshToken() {
    migrateLegacyLocalStorage();
    return safeGet(sessionStorage, 'refresh_token');
  },

  setAccessToken(token) {
    safeSet(sessionStorage, 'access_token', token);
    safeRemove(localStorage, 'access_token');
  },

  setRefreshToken(token) {
    safeSet(sessionStorage, 'refresh_token', token);
    safeRemove(localStorage, 'refresh_token');
  },

  setTokens(accessToken, refreshToken) {
    this.setAccessToken(accessToken);
    this.setRefreshToken(refreshToken);
  },

  getUser() {
    migrateLegacyLocalStorage();
    return safeGet(sessionStorage, 'user');
  },

  setUser(user) {
    const value = typeof user === 'string' ? user : JSON.stringify(user);
    safeSet(sessionStorage, 'user', value);
    safeRemove(localStorage, 'user');
  },

  clearUser() {
    safeRemove(sessionStorage, 'user');
    safeRemove(localStorage, 'user');
  },

  clear() {
    for (const key of AUTH_KEYS) {
      safeRemove(sessionStorage, key);
      safeRemove(localStorage, key);
    }
  },
};
