import { t } from "./i18n.js";
// Обёртка над REST API платформы. Два режима входа:
//  * "cookie" — вход пользователя: сессия в httpOnly-куке, CSRF-токен держим в sessionStorage;
//  * "token"  — вход по API-токену (для автоматизации): токен в sessionStorage.
// Всё стирается при закрытии вкладки; сама кука недоступна JavaScript.
const K = { token: "dsp_token", mode: "dsp_mode", csrf: "dsp_csrf" };
const get = (k) => { try { return sessionStorage.getItem(k); } catch { return null; } };
const put = (k, v) => { try { sessionStorage.setItem(k, v); } catch { /* режим без хранилища */ } };

// Несекретная отметка «вход уже был»: без неё при открытии страницы не пробуем найти сессию (иначе лишний 401 в консоли).
const HINT = "dsp_hint";
const hintGet = () => { try { return localStorage.getItem(HINT) === "1"; } catch { return false; } };
const hintSet = () => { try { localStorage.setItem(HINT, "1"); } catch { /* ignore */ } };
const hintClear = () => { try { localStorage.removeItem(HINT); } catch { /* ignore */ } };

export const session = {
  get hint() { return hintGet(); },
  get token() { return get(K.token); },
  get mode() { return get(K.mode); },
  get csrf() { return get(K.csrf); },
  setToken(token) { put(K.token, token); put(K.mode, "token"); },
  setCookieSession(csrf) { put(K.mode, "cookie"); hintSet(); if (csrf) put(K.csrf, csrf); },
  setCsrf(csrf) { if (csrf) put(K.csrf, csrf); },
  clear() { hintClear(); try { Object.values(K).forEach((k) => sessionStorage.removeItem(k)); } catch { /* ignore */ } },
};

export class ApiError extends Error {
  constructor(status, message, detail) { super(message); this.status = status; this.detail = detail; }
}

function detailText(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
  if (detail.error === "limit_reached") return t(`limitReached_${detail.limit}`, { max: detail.max, tier: detail.tier });
  if (detail.message) return detail.message;
  if (detail.error) return `${detail.error}${detail.feature ? `: ${detail.feature}` : ""}${detail.reason ? `: ${detail.reason}` : ""}`;
  return JSON.stringify(detail);
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export async function api(path, { method = "GET", body, raw = false, redirectOn401 = true } = {}) {
  const headers = {};
  if (session.mode === "token" && session.token) headers.Authorization = `Bearer ${session.token}`;
  if (session.mode === "cookie" && UNSAFE.has(method) && session.csrf) headers["X-CSRF-Token"] = session.csrf;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try { res = await fetch(path, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) }); }
  catch { throw new ApiError(0, t("networkDown")); }     // сервер недоступен (туннель закрыт, платформа перезапускается), а не «NetworkError when attempting…»
  if (res.status === 401 && redirectOn401) {
    session.clear();
    if (location.hash !== "#/login") location.hash = "#/login";
    throw new ApiError(401, "unauthorized");
  }
  if (raw) {
    if (!res.ok) throw new ApiError(res.status, res.statusText);
    return res;
  }
  const text = await res.text();
  let data = null;
  if (text) { try { data = JSON.parse(text); } catch { /* не JSON */ } }
  if (!res.ok) throw new ApiError(res.status, detailText(data && data.detail, res.statusText), data && data.detail);
  return data;
}

export const qs = (params) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, v);
  const s = p.toString();
  return s ? `?${s}` : "";
};
