/* 质量看板 — renders /api/quality-dashboard. CSP-safe: no inline handlers,
   dynamic bars via DOM style API (CSP does not gate element.style). */
(function () {
  "use strict";

  var VARIETY_LABELS = {
    standard_putonghua: "普通话",
    beijing_mandarin: "北京话",
    dongbei_mandarin: "东北话",
    sichuan_chongqing_mandarin: "川渝话",
    jianghuai_or_lower_yangtze_mandarin: "江淮话",
    guangdong_mandarin: "广东普通话",
    shanghai_mandarin_style: "上海话风格",
    cantonese_written: "粤语书面语",
    hokkien_written: "台湾闽南语",
    minnan_written: "福建闽南语"
  };

  function label(variety) {
    return VARIETY_LABELS[variety] || variety;
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function degradedBadge(rate) {
    var pct = (rate * 100).toFixed(1);
    var cls = rate === 0 ? "badge-ok" : rate < 0.1 ? "badge-warn" : "badge-bad";
    return el("span", "badge " + cls, pct + "%");
  }

  function meter(ratio, cls) {
    var wrap = el("span", "meter");
    var fill = el("span", "meter-fill" + (cls ? " " + cls : ""));
    fill.style.width = Math.max(0, Math.min(1, ratio)) * 100 + "%";
    wrap.appendChild(fill);
    return wrap;
  }

  function renderOps(ops) {
    var host = document.getElementById("ops-body");
    host.textContent = "";
    var varieties = Object.keys(ops);
    if (!varieties.length) {
      host.appendChild(el("p", "empty", "还没有改写请求记录 — 做一次改写后回来看。"));
      return;
    }
    var table = el("table");
    var head = el("tr");
    ["方言", "请求数", "降级", "降级率", "平均延迟"].forEach(function (h, i) {
      head.appendChild(el("th", i > 0 ? "num" : "", h));
    });
    var thead = el("thead");
    thead.appendChild(head);
    table.appendChild(thead);
    var tbody = el("tbody");
    var maxLatency = 1;
    varieties.forEach(function (v) {
      var lat = ops[v].latency_ms;
      if (lat && lat.mean > maxLatency) maxLatency = lat.mean;
    });
    varieties.sort().forEach(function (v) {
      var o = ops[v];
      var requests = o.requests || 0;
      var degraded = o.degraded || 0;
      var rate = requests ? degraded / requests : 0;
      var tr = el("tr");
      tr.appendChild(el("td", "variety-name", label(v)));
      tr.appendChild(el("td", "num", String(requests)));
      tr.appendChild(el("td", "num", String(degraded)));
      var rateTd = el("td", "num");
      rateTd.appendChild(degradedBadge(rate));
      tr.appendChild(rateTd);
      var latTd = el("td", "num");
      if (o.latency_ms && o.latency_ms.count) {
        var lvl = rate >= 0.1 ? "bad" : rate > 0 ? "warn" : "";
        latTd.appendChild(meter(o.latency_ms.mean / maxLatency, lvl));
        latTd.appendChild(document.createTextNode((o.latency_ms.mean / 1000).toFixed(1) + "s"));
      } else {
        latTd.textContent = "—";
      }
      tr.appendChild(latTd);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  function renderRatings(ratings) {
    var host = document.getElementById("ratings-body");
    host.textContent = "";
    if (!ratings.length) {
      host.appendChild(el("p", "empty", "还没有评分数据 — 评分按钮和 rate API 会写这里。"));
      return;
    }
    var table = el("table");
    var head = el("tr");
    ["对象", "场景", "次数", "均分", "σ", "最低", "最高"].forEach(function (h, i) {
      head.appendChild(el("th", i > 1 ? "num" : "", h));
    });
    var thead = el("thead");
    thead.appendChild(head);
    table.appendChild(thead);
    var tbody = el("tbody");
    ratings
      .slice()
      .sort(function (a, b) {
        return (a.variety + a.scenario).localeCompare(b.variety + b.scenario);
      })
      .forEach(function (b) {
        var tr = el("tr");
        var name = b.variety.indexOf("mode:") === 0 ? b.variety.slice(5) : label(b.variety);
        tr.appendChild(el("td", "variety-name", name));
        tr.appendChild(el("td", "", b.scenario));
        tr.appendChild(el("td", "num", String(b.stats.count)));
        tr.appendChild(el("td", "num", b.stats.mean.toFixed(2)));
        tr.appendChild(el("td", "num", b.stats.stddev.toFixed(2)));
        tr.appendChild(el("td", "num", b.stats.min === null ? "—" : String(b.stats.min)));
        tr.appendChild(el("td", "num", b.stats.max === null ? "—" : String(b.stats.max)));
        tbody.appendChild(tr);
      });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  function renderCorpus(corpus) {
    var host = document.getElementById("corpus-body");
    host.textContent = "";
    var summary = el("p", "corpus-summary");
    summary.appendChild(document.createTextNode("共 "));
    summary.appendChild(el("strong", "", String(corpus.total)));
    summary.appendChild(document.createTextNode(" 条语料,其中 "));
    summary.appendChild(el("strong", "", String(corpus.needs_review)));
    summary.appendChild(document.createTextNode(" 条待母语者复审。"));
    host.appendChild(summary);
    var byVariety = corpus.needs_review_by_variety || {};
    var keys = Object.keys(byVariety);
    if (!keys.length) {
      host.appendChild(el("p", "empty", "全部 verified — 语料库状态良好。"));
      return;
    }
    var chips = el("div", "chips");
    keys.sort().forEach(function (v) {
      var chip = el("span", "chip", label(v));
      chip.appendChild(el("span", "count", String(byVariety[v])));
      chips.appendChild(chip);
    });
    host.appendChild(chips);
  }

  fetch("/api/quality-dashboard")
    .then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    })
    .then(function (data) {
      renderOps(data.rewrite_ops || {});
      renderRatings(data.ratings || []);
      renderCorpus(data.corpus || { total: 0, needs_review: 0, needs_review_by_variety: {} });
    })
    .catch(function (err) {
      ["ops-body", "ratings-body", "corpus-body"].forEach(function (id) {
        var host = document.getElementById(id);
        host.textContent = "";
        host.appendChild(el("p", "empty", "加载失败:" + err.message));
      });
    });
})();
