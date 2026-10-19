/* 管理后台核心（D3-C）：CSRF / API 助手 / 登出 / 提示净化。
   - 令牌只在 meta 标签内存中（不落 localStorage）
   - 401 -> 回登录页；403 -> 提示（含强制改密门）
   - 本文件不包含任何业务逻辑
   - plain()：失败原因兜底净化——识别不出或含技术术语时转通俗提示，
     保证界面不出现底层报错与技术名词 */
(function () {
  "use strict";

  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = meta ? meta.getAttribute("content") || "" : "";

  var GENERIC = "操作未成功，请刷新页面后重试";
  var TECH_PATTERN = /(payload|json|sql|null|undefined|traceback|exception|stack|csrf|http\s*\d{3}|internal|server error|validation)/i;

  function api(path, options) {
    options = options || {};
    options.credentials = "same-origin";
    options.headers = options.headers || {};
    if (csrfToken) { options.headers["X-CSRF-Token"] = csrfToken; }
    if (options.body && typeof options.body === "object") {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }
    return fetch(path, options).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (resp.status === 401) {
          location.href = "/login";
          return { status: resp.status, data: data };
        }
        return { status: resp.status, data: data };
      });
    });
  }

  /* 失败原因 -> 通俗提示。非字符串（如框架校验数组）或含技术术语 -> 兜底。 */
  function plain(detail, fallback) {
    var fb = fallback || GENERIC;
    if (typeof detail !== "string") { return fb; }
    var text = detail.trim();
    if (!text || TECH_PATTERN.test(text)) { return fb; }
    return text;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
               "'": "&#39;" }[c];
    });
  }

  function msg(id, text, ok) {
    var el = typeof id === "string" ? document.getElementById(id) : id;
    if (!el) { return; }
    el.textContent = plain(text, ok ? "" : GENERIC);
    el.className = "msg " + (ok ? "ok" : "err");
  }

  window.GOPS = {
    api: api,
    esc: esc,
    msg: msg,
    plain: plain,
    csrf: function () { return csrfToken; }
  };

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("logout-btn");
    if (btn) {
      btn.addEventListener("click", function () {
        api("/api/admin/logout", { method: "POST" }).then(function () {
          location.href = "/login";
        });
      });
    }
  });
})();
