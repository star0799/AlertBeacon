// ==UserScript==
// @name         SDC Token Sync (after manual login) - fetch+XHR
// @version      2026.08.30.1
// @match        *://sdr.stardreamcruises.com/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

(function () {
  const SCRIPT_VERSION = "2026.08.30.1";
  const LOADED_KEY = "__sdcTokenSyncLoadedVersion";
  if (window[LOADED_KEY]) {
    console.log("[SDC] TokenSync already loaded version=", window[LOADED_KEY]);
    return;
  }
  window[LOADED_KEY] = SCRIPT_VERSION;

  console.log("[SDC] TokenSync script loaded version=", SCRIPT_VERSION, "on", location.href);
  const PUSH_URL = "http://127.0.0.1:5000/cruise/tokens";
  const TARGET = "/auth/customer/login";

  function log(...args) { console.log("[SDC]", ...args); }

  function postTokens(accessToken, refreshToken, user) {
    GM_xmlhttpRequest({
      method: "POST",
      url: PUSH_URL,
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({
        accessToken,
        refreshToken,
        user,
        at: new Date().toISOString(),
        source: "browser_token_sync",
        script_version: SCRIPT_VERSION,
        page_url: location.href,
        visibility: document.visibilityState,
      }),
      timeout: 8000,
      onload: (res) => log("tokens push status=", res.status),
      onerror: (e) => log("tokens push error", e),
      ontimeout: () => log("tokens push timeout"),
    });
  }

  function tryHandleJson(text, via) {
    try {
      const data = JSON.parse(text);
      if (data?.accessToken && data?.refreshToken) {
        log("tokens captured via", via);
        postTokens(data.accessToken, data.refreshToken, data.user);
        log("tokens synced");
        return true;
      }
    } catch {}
    return false;
  }

  // ---- fetch hook ----
  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    const res = await origFetch.apply(this, arguments);
    try {
      const url = typeof input === "string" ? input : input.url;
      if (url && url.includes(TARGET)) {
        const cloned = res.clone();
        const text = await cloned.text();
        tryHandleJson(text, "fetch");
      }
    } catch (e) {
      log("fetch hook error", e);
    }
    return res;
  };

  // ---- XHR hook ----
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url) {
    this.__sdc_url = url;
    return origOpen.apply(this, arguments);
  };

  XMLHttpRequest.prototype.send = function (body) {
    try {
      const url = this.__sdc_url || "";
      if (typeof url === "string" && url.includes(TARGET)) {
        this.addEventListener("load", function () {
          try {
            // responseText 可能是 json 字串
            const text = this.responseText;
            if (text) tryHandleJson(text, "xhr");
          } catch (e) {
            log("xhr parse error", e);
          }
        });
      }
    } catch (e) {
      log("xhr hook error", e);
    }
    return origSend.apply(this, arguments);
  };

  log("token sync loaded on", location.href);
})();
