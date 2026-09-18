/* Tiny helpers: toast + htmx error surfacing. No framework. */
(function () {
  var timer = null;

  function toast(message, isError) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.classList.toggle("error", !!isError);
    el.hidden = false;
    clearTimeout(timer);
    timer = setTimeout(function () { el.hidden = true; }, isError ? 5000 : 2500);
  }
  window.toast = toast;

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
})();
