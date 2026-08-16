// 把 data-utc 里的 UTC 时间转成用户当地时间显示。
// 数据库统一存 UTC；本地化只在前端发生。
//
// 相对时间的措辞跟随界面语言：两种写法一起写进 DOM（见 NA.setText），
// 切换语言时不用重算。悬停提示是 title 属性、放不下两份，才需要监听语言变化。
(function () {
  function rel(d, now, en) {
    var mins = Math.floor((now - d) / 60000);
    if (mins < 1) return en ? "just now" : "刚刚";
    if (mins < 60) return en ? (mins + (mins > 1 ? " mins ago" : " min ago")) : (mins + " 分钟前");
    var h = Math.floor(mins / 60);
    if (h < 24) return en ? (h + (h > 1 ? " hrs ago" : " hr ago")) : (h + " 小时前");
    var days = Math.floor(h / 24);
    return en ? (days + (days > 1 ? " days ago" : " day ago")) : (days + " 天前");
  }

  function local(d, en) {
    return d.toLocaleString(en ? "en-US" : "zh-CN", {
      month: "numeric", day: "numeric",
      hour: "2-digit", minute: "2-digit"
    });
  }

  var now = new Date();
  var els = [];
  document.querySelectorAll("time.ts[data-utc]").forEach(function (el) {
    var d = new Date(el.getAttribute("data-utc"));
    if (isNaN(d)) { el.textContent = ""; return; }
    if (window.NA) {
      NA.setText(el, rel(d, now, false), rel(d, now, true));   // 列表里显示相对时间
    } else {
      el.textContent = rel(d, now, false);
    }
    el.setAttribute("datetime", d.toISOString());
    els.push([el, d]);
  });

  function paintTitles(lang) {
    var en = lang === "en";
    els.forEach(function (p) { p[0].title = local(p[1], en); });   // 悬停显示当地绝对时间
  }
  paintTitles(window.NA ? NA.lang() : "zh");
  if (window.NA) NA.onChange(paintTitles);
})();
