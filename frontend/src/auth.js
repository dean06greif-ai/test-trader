// Minimal single-admin auth helper (token in localStorage) with 1-day expiry.
const TOKEN_KEY = 'admin_token';
const TOKEN_TS_KEY = 'admin_token_ts';
const ONE_DAY_MS = 24 * 60 * 60 * 1000;

export const setToken = (t) => {
  localStorage.setItem(TOKEN_KEY, t);
  localStorage.setItem(TOKEN_TS_KEY, String(Date.now()));
};

export const clearToken = () => {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(TOKEN_TS_KEY);
};

export const getToken = () => {
  const t = localStorage.getItem(TOKEN_KEY);
  if (!t) return null;
  const ts = parseInt(localStorage.getItem(TOKEN_TS_KEY) || '0', 10);
  if (!ts || Date.now() - ts > ONE_DAY_MS) {
    clearToken();
    return null;
  }
  return t;
};

export const isAdmin = () => !!getToken();

export const authHeaders = () => {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
};
