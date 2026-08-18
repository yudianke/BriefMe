// 左侧「新闻来源」筛选。同时用于地区页（分类网格）和分类详情页（文章列表）。
//
// 存的是「被排除的来源」而不是「被选中的来源」。原因：分类页只列出该分类
// 出现过的媒体，若存选中列表，在分类页全选存回去时会漏掉本页没有的媒体，
// 回到地区页就表现为那些媒体被莫名取消勾选。存排除项则天然不受页面差异影响，
// 并且要保留「不在本页的排除项」，避免来回切换时丢失用户在别处的选择。
(function () {
  var bar = document.getElementById('srcbar');
  if (!bar) return;

  var region = document.body.dataset.region || 'default';
  // 期刊页筛的是「本」刊，新闻页筛的是「家」媒体——只是提示文案的量词不同。
  // 用 data-region 判断而不是靠 sections.length：总览页没有 .jr-journal 区块，
  // 但它同样是期刊页，量词也该是「本」。
  var isJournals = region === 'journals';
  var KEY = 'newsagg.hidden.' + region;
  var boxes = [].slice.call(bar.querySelectorAll('.srcbox'));
  var cards = [].slice.call(document.querySelectorAll('.cat-card'));               // 地区页
  var articles = [].slice.call(document.querySelectorAll('article[data-source]')); // 分类页
  // 「分组成整块」的页面（学科页按期刊、按来源页按媒体）：筛选就是整块显隐。
  // 用 data-src-section 这个与外观无关的标记，而不是绑死某个 class——
  // 两种页面视觉差别很大，但筛选行为完全一致。
  var sections = [].slice.call(document.querySelectorAll('[data-src-section]'));
  var tip = document.getElementById('srcTip');
  var allHidden = document.getElementById('allHidden');
  var toggleBtn = document.getElementById('srcToggle');
  var artCount = document.getElementById('artCount');

  // i18n.js 若因故没加载，退化成只写中文，筛选功能本身不受影响
  var setText = (window.NA && NA.setText) ||
                function (el, zh) { if (el) el.textContent = zh; };

  var onPage = boxes.map(function (b) { return b.value; });
  var hidden = [];
  try {
    var saved = JSON.parse(localStorage.getItem(KEY));
    if (Array.isArray(saved)) hidden = saved;
  } catch (e) { /* 存储不可用就当作没有排除项 */ }

  boxes.forEach(function (b) { b.checked = hidden.indexOf(b.value) === -1; });

  function persist() {
    // 本页之外的排除项原样保留，只覆盖本页这些来源的状态
    var offPage = hidden.filter(function (id) { return onPage.indexOf(id) === -1; });
    var offHere = boxes.filter(function (b) { return !b.checked; })
                       .map(function (b) { return b.value; });
    hidden = offPage.concat(offHere);
    try { localStorage.setItem(KEY, JSON.stringify(hidden)); } catch (e) { /* 忽略 */ }
  }

  function apply() {
    var set = {};
    boxes.forEach(function (b) { if (b.checked) set[b.value] = true; });
    var on = boxes.filter(function (b) { return b.checked; }).length;

    // ---- 地区页：逐条显隐预览、更新计数徽章、隐藏空分类 ----
    cards.forEach(function (card) {
      var shown = 0;
      // 卡片里渲染的预览条数多于要显示的：除了「该分类最新 N 条」，还额外
      // 塞了每家媒体各自的最新几条（默认隐藏）。这里只放出当前筛选下最新的
      // limit 条——条目本身按时间倒序渲染，所以顺着取就是最新的。
      // 这样勾选一家原本没进前 N 的媒体时，卡片不会变成「徽章 26 条、预览 0 条」。
      var limit = +(card.dataset.preview || 6);
      [].forEach.call(card.querySelectorAll('li[data-source]'), function (li) {
        var ok = !!set[li.dataset.source] && !(trivialOn && li.dataset.trivial === '1');
        if (ok && shown < limit) { li.hidden = false; shown++; }
        else { li.hidden = true; }
      });
      // 徽章用该分类下各家媒体的真实条数累加，而不是预览里可见的条数
      var total = 0;
      try {
        var counts = JSON.parse(card.dataset.counts || '{}');
        var tcounts = JSON.parse(card.dataset.trivialCounts || '{}');
        boxes.forEach(function (b) {
          if (!b.checked) return;
          total += counts[b.value] || 0;
          if (trivialOn) total -= tcounts[b.value] || 0;
        });
      } catch (e) { total = shown; }
      var badge = card.querySelector('.count');
      var moreN = card.querySelector('.more-n');
      if (badge) badge.textContent = total;
      if (moreN) moreN.textContent = total;
      card.hidden = total === 0 && shown === 0;
    });

    // ---- 学科页：整块显隐期刊、累加可见论文数 ----
    var visiblePapers = 0;
    sections.forEach(function (sec) {
      var ok = !!set[sec.dataset.source];
      sec.hidden = !ok;
      if (!ok) return;
      // 期刊页的条目是 .paper，按来源页是带 data-trivial 的 li；
      // 后者还要受「隐藏琐碎新闻」开关影响，所以逐条算而不是直接取长度。
      var items = sec.querySelectorAll('.paper, li[data-trivial]');
      if (!items.length) { visiblePapers += sec.querySelectorAll('li').length; return; }
      [].forEach.call(items, function (it) {
        var show = !(trivialOn && it.dataset.trivial === '1');
        it.hidden = !show;
        if (show) visiblePapers++;
      });
    });
    if (sections.length && artCount) artCount.textContent = visiblePapers;

    // ---- 分类页：逐篇显隐文章、更新标题栏总数 ----
    var visibleArts = 0;
    articles.forEach(function (art) {
      var ok = !!set[art.dataset.source] && !(trivialOn && art.dataset.trivial === '1');
      art.hidden = !ok;
      if (ok) visibleArts++;
    });
    if (artCount && !sections.length) artCount.textContent = visibleArts;

    if (allHidden) allHidden.hidden = on !== 0;
    // 这几处文本由 JS 现算，模板管不到，所以用 NA.setText 一次写入中英两份
    if (tip) {
      var sel = on + '/' + boxes.length;
      if (on === boxes.length) {
        if (isJournals) {
          setText(tip, '显示全部 ' + boxes.length + ' 本',
                       'Showing all ' + boxes.length + ' journals');
        } else {
          setText(tip, '显示全部 ' + boxes.length + ' 家',
                       'Showing all ' + boxes.length + ' sources');
        }
      } else if (sections.length) {
        // 整块显隐的页面有两种：期刊学科页（本/papers）与新闻按来源页（家/articles）。
        // 量词得跟着页面走，否则中国新闻的来源页会写成「5/6 本 · 781 papers」。
        setText(tip,
          '已选 ' + sel + (isJournals ? ' 本 · ' : ' 家 · ') + visiblePapers + ' 篇',
          sel + (isJournals ? ' journals · ' : ' sources · ') + visiblePapers +
          (isJournals ? ' papers' : ' articles'));
      } else if (isJournals) {
        var visJ = cards.filter(function (c) { return !c.hidden; }).length;
        setText(tip, '已选 ' + sel + ' 本 · ' + visJ + ' 个学科',
                     sel + ' journals · ' + visJ + ' disciplines');
      } else if (articles.length) {
        setText(tip, '已选 ' + sel + ' 家 · ' + visibleArts + ' 篇',
                     sel + ' sources · ' + visibleArts + ' articles');
      } else {
        var visCards = cards.filter(function (c) { return !c.hidden; }).length;
        setText(tip, '已选 ' + sel + ' 家 · ' + visCards + ' 个分类',
                     sel + ' sources · ' + visCards + ' categories');
      }
    }
    if (toggleBtn) {
      var all = on === boxes.length;
      setText(toggleBtn, all ? '全不选' : '全选', all ? 'None' : 'All');
    }
  }

  // ---- 「隐藏琐碎新闻」开关（默认关闭）----
  var TKEY = 'newsagg.hideTrivial';
  var trivialOn = false;
  try { trivialOn = localStorage.getItem(TKEY) === '1'; } catch (e) { /* 忽略 */ }
  var trivialBtn = document.getElementById('trivialToggle');

  function paintTrivialBtn() {
    if (!trivialBtn) return;
    trivialBtn.classList.toggle('on', trivialOn);
    trivialBtn.setAttribute('aria-pressed', trivialOn ? 'true' : 'false');
    // title 是属性、塞不下两份文本，只能按当前语言现算
    var en = window.NA ? NA.lang() === 'en' : false;
    trivialBtn.title = trivialOn
      ? (en ? 'Trivia hidden — click to show everything' : '当前：已隐藏琐碎新闻，点击恢复显示')
      : (en ? 'Showing everything — click to hide trivia' : '当前：显示全部，点击隐藏琐碎新闻');
  }
  if (window.NA) NA.onChange(paintTrivialBtn);

  if (trivialBtn) {
    trivialBtn.addEventListener('click', function () {
      trivialOn = !trivialOn;
      try { localStorage.setItem(TKEY, trivialOn ? '1' : '0'); } catch (e) { /* 忽略 */ }
      paintTrivialBtn();
      apply();
    });
    paintTrivialBtn();
  }

  boxes.forEach(function (b) {
    b.addEventListener('change', function () { persist(); apply(); });
  });

  if (toggleBtn) {
    toggleBtn.addEventListener('click', function () {
      var toAll = boxes.filter(function (b) { return b.checked; }).length !== boxes.length;
      boxes.forEach(function (b) { b.checked = toAll; });
      persist();
      apply();
    });
  }

  apply();
})();
