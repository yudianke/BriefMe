// 中文 / English 切换。
//
// 页面里中英两份文本都已经由模板输出，显示哪一份完全由 <html data-lang> + CSS 决定，
// 所以切换本身不需要 JS 参与。这个文件只负责三件 CSS 做不到的事：
//   1) 记住用户的选择（localStorage）
//   2) 换 <title> 和 <html lang>（不是普通元素，CSS 管不着）
//   3) 给需要动态写文本的脚本（srcfilter/localtime）提供双语写入工具
//
// 首屏语言由 <head> 里的内联脚本抢先设置，本文件在页尾加载，
// 因此不会出现「先闪一下中文再变英文」。
(function () {
  var KEY = 'newsagg.lang';
  var root = document.documentElement;
  var titleEl = document.querySelector('title');
  var titleZh = titleEl ? titleEl.textContent : '';
  var titleEn = titleEl ? (titleEl.getAttribute('data-en') || titleZh) : '';
  var listeners = [];

  function current() {
    return root.getAttribute('data-lang') === 'en' ? 'en' : 'zh';
  }

  function apply(lang) {
    root.setAttribute('data-lang', lang);
    root.lang = (lang === 'en') ? 'en' : 'zh-CN';
    if (titleEl) document.title = (lang === 'en') ? titleEn : titleZh;
    listeners.forEach(function (fn) { try { fn(lang); } catch (e) { /* 单个监听失败不影响其余 */ } });
  }

  window.NA = {
    lang: current,

    // 往元素里写双语文本。两份都写进 DOM，由 CSS 决定显示哪份——
    // 这样语言切换时不必重跑写入逻辑。用 textContent 而非 innerHTML，
    // 即使将来传入的是媒体名之类的外部数据也不会有注入问题。
    setText: function (el, zh, en) {
      if (!el) return;
      var z = el.querySelector('.i18n-zh');
      var e = el.querySelector('.i18n-en');
      if (!z || !e) {
        el.textContent = '';
        z = document.createElement('span'); z.className = 'i18n-zh';
        e = document.createElement('span'); e.className = 'i18n-en';
        el.appendChild(z); el.appendChild(e);
      }
      z.textContent = zh;
      e.textContent = en;
    },

    // 给必须按语言重算的东西用（比如 title 属性这种没法放两份的地方）
    onChange: function (fn) { listeners.push(fn); }
  };

  // data-lang 已由内联脚本设好，这里补上 <title> 与 lang 属性
  apply(current());

  var btn = document.getElementById('langToggle');
  if (btn) {
    btn.addEventListener('click', function () {
      var next = (current() === 'en') ? 'zh' : 'en';
      try { localStorage.setItem(KEY, next); } catch (e) { /* 存储不可用就只在本页生效 */ }
      apply(next);
    });
  }
})();
