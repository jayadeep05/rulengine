/**
 * auth-client.js
 * ──────────────
 * Drop-in auth client for every dashboard page.
 *
 * Features:
 *  - Attaches JWT Authorization header to every fetch() automatically
 *  - Auto-refreshes the access token when it's about to expire (5 min buffer)
 *  - Redirects to /login on 401 (session expired)
 *  - Injects a persistent logout button into the page
 *  - Works with BOTH HTTP-only cookie flow AND localStorage fallback
 *
 * Include ONCE at the top of each protected HTML page:
 *   <script src="/static/auth-client.js"></script>
 */

(function () {
  "use strict";

  // ── Token storage helpers ──────────────────────────────────────────────────
  function getToken() {
    return localStorage.getItem("_ib_token") || sessionStorage.getItem("_ib_token");
  }

  function setToken(t) {
    if (localStorage.getItem("_ib_token") !== null) {
      localStorage.setItem("_ib_token", t);
    } else {
      sessionStorage.setItem("_ib_token", t);
    }
  }

  function clearToken() {
    localStorage.removeItem("_ib_token");
    sessionStorage.removeItem("_ib_token");
  }

  // ── Decode JWT payload (no validation — server validates) ─────────────────
  function decodePayload(token) {
    try {
      const b64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      return JSON.parse(atob(b64));
    } catch {
      return null;
    }
  }

  // ── Refresh logic ─────────────────────────────────────────────────────────
  let _refreshPromise = null;

  async function tryRefresh() {
    if (_refreshPromise) return _refreshPromise;
    _refreshPromise = (async () => {
      try {
        const r = await window._origFetch("/refresh", {
          method: "POST", credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        if (r.ok) {
          const d = await r.json();
          if (d.access_token) {
            setToken(d.access_token);
            return d.access_token;
          }
        }
      } catch {}
      return null;
    })().finally(() => { _refreshPromise = null; });
    return _refreshPromise;
  }

  // ── Auto-refresh every 50 minutes (before 60-min expiry) ─────────────────
  function scheduleRefresh(token) {
    const payload = decodePayload(token);
    if (!payload || !payload.exp) return;
    const msUntilExpiry = payload.exp * 1000 - Date.now();
    const refreshAt     = msUntilExpiry - 5 * 60 * 1000; // 5-min buffer
    if (refreshAt > 0) {
      setTimeout(async () => {
        const newTok = await tryRefresh();
        if (newTok) scheduleRefresh(newTok);
        else redirectToLogin();
      }, refreshAt);
    }
  }

  function getApiBase() {
    if (window.AndroidBridge && typeof window.AndroidBridge.getServerUrl === 'function') {
      const s = window.AndroidBridge.getServerUrl();
      if (s && !s.startsWith("file:")) return s;
    }
    const saved = localStorage.getItem("_ib_server_url");
    if (saved) return saved;
    if (window.location.origin && window.location.origin !== "null" && !window.location.origin.startsWith("file:")) {
      return window.location.origin;
    }
    return "http://10.0.2.2:8000";
  }

  function redirectToLogin() {
    clearToken();
    if (window.location.protocol === "file:") {
      window.location.href = "login.html";
    } else {
      window.location.href = "/login";
    }
  }

  // ── Intercept fetch() ──────────────────────────────────────────────────────
  window._origFetch = window.fetch;
  window.fetch = async function (input, init = {}) {
    if (typeof input === "string" && input.startsWith("/")) {
      input = getApiBase() + input;
    }
    const token = getToken();
    if (token) {
      init.headers = { ...(init.headers || {}), "Authorization": "Bearer " + token };
    }
    init.credentials = init.credentials || "include";

    let resp = await window._origFetch(input, init);

    if (resp.status === 401) {
      // Try refresh once
      const newTok = await tryRefresh();
      if (newTok) {
        init.headers = { ...(init.headers || {}), "Authorization": "Bearer " + newTok };
        resp = await window._origFetch(input, init);
        if (resp.status === 401) { redirectToLogin(); }
      } else {
        redirectToLogin();
      }
    }
    return resp;
  };

  // ── Session verification on page load ────────────────────────────────────
  async function verifySelf() {
    try {
      const r = await fetch("/me");
      if (!r.ok) redirectToLogin();
    } catch {
      redirectToLogin();
    }
  }

  // ── Logout ────────────────────────────────────────────────────────────────
  async function logout() {
    try {
      await fetch("/logout", { method: "POST" });
    } catch {}
    clearToken();
    redirectToLogin();
  }

  // ── Inject logout button ──────────────────────────────────────────────────
  // Logout button is now embedded in the dashboard header — no floating button needed.
  function injectLogoutButton() { /* no-op: button is in header */ }

  // ── Init ──────────────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", async () => {
    const token = getToken();
    if (token) scheduleRefresh(token);
    await verifySelf();
  });

  // Export for manual use
  window.authClient = { logout, getToken, setToken, clearToken };

})();
