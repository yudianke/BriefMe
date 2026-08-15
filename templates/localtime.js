// 把 data-utc 里的 UTC 时间转成用户当地时间显示。
// 数据库统一存 UTC；本地化只在前端发生。
(function () {
  function rel(d, now) {
    var mins = Math.floor((now - d) / 60000);
    if (mins < 1) return "刚刚";
    if (mins < 60) return mins + " 分钟前";
    var h = Math.floor(mins / 60);
    if (h < 24) return h + " 小时前";
    return Math.floor(h / 24) + " 天前";
  }

  function local(d) {
    return d.toLocaleString(undefined, {
      month: "numeric", day: "numeric",
      hour: "2-digit", minute: "2-digit"
    });
  }

  var now = new Date();
  document.querySelectorAll("time.ts[data-utc]").forEach(function (el) {
    var d = new Date(el.getAttribute("data-utc"));
    if (isNaN(d)) { el.textContent = ""; return; }
    el.textContent = rel(d, now);          // 列表里显示相对时间
    el.title = local(d);                   // 悬停显示当地绝对时间
    el.setAttribute("datetime", d.toISOString());
  });
})();
