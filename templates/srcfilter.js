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
  var KEY = 'newsagg.hidden.' + region;
  var boxes = [].slice.call(bar.querySelectorAll('.srcbox'));
  var cards = [].slice.call(document.querySelectorAll('.cat-card'));               // 地区页
  var articles = [].slice.call(document.querySelectorAll('article[data-source]')); // 分类页
  var tip = document.getElementById('srcTip');
  var allHidden = document.getElementById('allHidden');
  var toggleBtn = document.getElementById('srcToggle');
  var artCount = document.getElementById('artCount');

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
      [].forEach.call(card.querySelectorAll('li[data-source]'), function (li) {
        var ok = !!set[li.dataset.source] && !(trivialOn && li.dataset.trivial === '1');
        li.hidden = !ok;
        if (ok) shown++;
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

    // ---- 分类页：逐篇显隐文章、更新标题栏总数 ----
    var visibleArts = 0;
    articles.forEach(function (art) {
      var ok = !!set[art.dataset.source] && !(trivialOn && art.dataset.trivial === '1');
      art.hidden = !ok;
      if (ok) visibleArts++;
    });
    if (artCount) artCount.textContent = visibleArts;

    if (allHidden) allHidden.hidden = on !== 0;
    if (tip) {
      if (on === boxes.length) {
        tip.textContent = '显示全部 ' + boxes.length + ' 家';
      } else if (articles.length) {
        tip.textContent = '已选 ' + on + '/' + boxes.length + ' 家 · ' + visibleArts + ' 篇';
      } else {
        var visCards = cards.filter(function (c) { return !c.hidden; }).length;
        tip.textContent = '已选 ' + on + '/' + boxes.length + ' 家 · ' + visCards + ' 个分类';
      }
    }
    if (toggleBtn) toggleBtn.textContent = on === boxes.length ? '全不选' : '全选';
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
    trivialBtn.title = trivialOn ? '当前：已隐藏琐碎新闻，点击恢复显示' : '当前：显示全部，点击隐藏琐碎新闻';
  }

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
