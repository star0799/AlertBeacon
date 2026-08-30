// ==UserScript==
// @name         SDC Token Sync (after manual login) - fetch+XHR
// @version      1.0.0
// @description  Loads the latest SDC token sync script from AlertBeacon.
// @match        *://sdr.stardreamcruises.com/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @updateURL    http://127.0.0.1:5000/cruise/sdc-token-sync-loader.user.js
// @downloadURL  http://127.0.0.1:5000/cruise/sdc-token-sync-loader.user.js
// ==/UserScript==

(function () {
  "use strict";

  const SCRIPT_URL = "http://127.0.0.1:5000/cruise/sdc-token-sync.js";
  const MAX_ATTEMPTS = 20;
  const RETRY_DELAY_MS = 3000;

  function log(...args) {
    console.log("[SDC Loader]", ...args);
  }

  function retry(attempt, reason) {
    if (attempt >= MAX_ATTEMPTS) {
      console.error("[SDC Loader] Unable to load local script after retries:", reason);
      return;
    }
    log(`load attempt ${attempt} failed; retrying in ${RETRY_DELAY_MS / 1000}s`, reason);
    setTimeout(() => loadScript(attempt + 1), RETRY_DELAY_MS);
  }

  function executeScript(source) {
    const run = new Function(
      "GM_xmlhttpRequest",
      `${source}\n//# sourceURL=sdc-token-sync.local.js`,
    );
    run(GM_xmlhttpRequest);
  }

  function loadScript(attempt) {
    GM_xmlhttpRequest({
      method: "GET",
      url: `${SCRIPT_URL}?_=${Date.now()}`,
      headers: { "Cache-Control": "no-cache" },
      timeout: 5000,
      onload: (response) => {
        if (response.status < 200 || response.status >= 300) {
          retry(attempt, `HTTP ${response.status}`);
          return;
        }
        try {
          executeScript(response.responseText);
          log("latest local script loaded");
        } catch (error) {
          retry(attempt, error);
        }
      },
      onerror: (error) => retry(attempt, error),
      ontimeout: () => retry(attempt, "timeout"),
    });
  }

  loadScript(1);
})();
