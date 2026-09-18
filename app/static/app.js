/* Tiny helpers: bottom sheet, toast auto-hide, htmx error surfacing, local times.
   No framework, no build step. */
(function () {
  "use strict";

  var toastTimer = null;

  function armToast() {
    var el = document.getElementById("toast");
    if (!el || el.hidden || !el.textContent.trim()) return;
    clearTimeout(toastTimer);
    var isError = el.classList.contains("error");
    toastTimer = setTimeout(function () { el.hidden = true; }, isError ? 6000 : 3000);
  }

  function toast(message, isError) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.classList.toggle("error", !!isError);
    el.hidden = false;
    armToast();
  }
  window.toast = toast;

  function closeSheet() {
    var sheet = document.getElementById("sheet");
    if (sheet) sheet.innerHTML = "";
  }
  window.closeSheet = closeSheet;

  /* Local time for <time data-local datetime="ISO">; runs on load and after every swap. */
  function localizeTimes(root) {
    var nodes = (root || document).querySelectorAll("time[data-local][datetime]");
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var iso = node.getAttribute("datetime");
      if (!iso || node.getAttribute("data-localized") === "1") continue;
      var date = new Date(iso);
      if (isNaN(date.getTime())) continue;
      try {
        node.textContent = date.toLocaleString(undefined, {
          weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"
        });
        node.title = iso;
        node.setAttribute("data-localized", "1");
      } catch (err) { /* keep the server text */ }
    }
  }

  /* Maker/taker toggle in the bet form prefills the price with the ask or the limit price. */
  function syncPriceWithMode(radio) {
    var form = radio.closest("form");
    if (!form) return;
    var price = form.querySelector('input[name="price"]');
    if (!price) return;
    var value = radio.value === "maker" ? price.getAttribute("data-price-maker")
                                        : price.getAttribute("data-price-taker");
    if (value) price.value = value;
  }

  document.addEventListener("click", function (evt) {
    var target = evt.target.closest("[data-close-sheet]");
    if (target) { evt.preventDefault(); closeSheet(); }
  });
  document.addEventListener("keydown", function (evt) {
    if (evt.key === "Escape") closeSheet();
  });
  document.addEventListener("change", function (evt) {
    var el = evt.target;
    if (el && el.matches && el.matches('input[type="radio"][name="mode"]')) syncPriceWithMode(el);
  });

  document.addEventListener("htmx:responseError", function (evt) {
    var status = evt.detail && evt.detail.xhr ? evt.detail.xhr.status : "?";
    toast("Request failed (" + status + ")", true);
  });
  document.addEventListener("htmx:sendError", function () {
    toast("Network error", true);
  });
  document.addEventListener("htmx:afterRequest", function (evt) {
    var xhr = evt.detail && evt.detail.xhr;
    if (!xhr) return;
    var msg = xhr.getResponseHeader("X-Toast");
    if (msg) toast(msg, false);
  });
  /* Server-rendered toasts arrive as an out-of-band swap of #toast; hide them after a moment. */
  document.addEventListener("htmx:oobAfterSwap", function () { armToast(); });
  document.addEventListener("htmx:afterSettle", function (evt) {
    armToast();
    localizeTimes(evt.target || document);
    var focus = document.querySelector('#sheet input[name="stake_usd"]');
    if (focus && evt.target && evt.target.id === "sheet") focus.focus();
  });

  document.addEventListener("DOMContentLoaded", function () { localizeTimes(document); });
  if (document.readyState !== "loading") localizeTimes(document);
})();
