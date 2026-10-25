/* Logout button wiring (Task 15). The logout button itself is shared
 * markup in base.html, present on every page (including public ones,
 * where this is effectively a no-op since there's no cached Basic Auth
 * credential for the current page's realm to discard).
 *
 * There is no server-side session/logout endpoint (no backend, per the
 * plan's Solution Overview) — Basic Auth credentials are cached by the
 * *browser*, keyed by realm, until the browser itself is closed. The only
 * way to make a browser discard a cached credential is the classic trick:
 * issue a request to the current page's own URL with deliberately wrong
 * credentials attached. Browsers vary in exactly how they react (hence
 * the Post-Completion manual cross-browser check), but the common
 * behavior is that supplying bad credentials via XMLHttpRequest.open()
 * causes the browser to drop/replace its cached-good credential for that
 * realm, so the *next* request (including the redirect below, and any
 * subsequent visit) prompts fresh.
 */
(function () {
  "use strict";

  function redirectToIndex() {
    window.location.href = "/";
  }

  function logout() {
    var xhr = new XMLHttpRequest();
    // A fresh, deliberately-wrong password each click (rather than a
    // fixed constant) avoids a browser treating a repeated identical
    // failed attempt as already-known/cacheable.
    var bogusPassword = "logout-" + Date.now() + "-" + Math.random().toString(36).slice(2);

    try {
      xhr.open("GET", window.location.href, true, "logout", bogusPassword);
    } catch (err) {
      redirectToIndex();
      return;
    }

    xhr.timeout = 3000;
    xhr.onload = redirectToIndex;
    xhr.onerror = redirectToIndex;
    xhr.ontimeout = redirectToIndex;
    xhr.onabort = redirectToIndex;

    try {
      xhr.send();
    } catch (err) {
      redirectToIndex();
    }
  }

  function init() {
    var button = document.getElementById("logout-button");
    if (!button) {
      return;
    }
    button.addEventListener("click", logout);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
