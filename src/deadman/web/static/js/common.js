// deadman 前端共享工具库（原各 HTML 内联重复实现收敛于此）。
// 页面在加载本文件前可设 window.APP_TOKEN_KEY（默认 org_token）指定 localStorage 会话键。
(function () {
  window.APP_TOKEN_KEY = window.APP_TOKEN_KEY || "org_token";
  window.el = function (id) { return document.getElementById(id); };
  window.token = function () { return localStorage.getItem(window.APP_TOKEN_KEY) || ""; };
  // 统一 fetch 封装：带 JSON 头 + Bearer，返回 {ok,status,data}
  window.api = function (path, opts) {
    opts = opts || {};
    opts.headers = Object.assign(
      { "Content-Type": "application/json", Authorization: "Bearer " + window.token() },
      opts.headers || {}
    );
    return fetch(path, opts).then(async function (r) {
      var d = null;
      try { d = await r.json(); } catch (e) {}
      return { ok: r.ok, status: r.status, data: d };
    });
  };
  // HTML 转义
  window.esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  };
  // unix 秒 → YYYY-MM-DD（统一日期格式）
  window.fmtDate = function (ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  };
})();
