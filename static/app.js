/* ============================================================
   地道中文 · v2 「墨韵」 前端逻辑（重写版）
   - 所有方言数据从 /api/presets 拉取，前端不再维护重复表
   - 收藏（favorite）真实落地到 localStorage
   - 历史按 (target_variety, normalized_text) 去重
   - 闽南语等无 TTS 的方言禁用朗读按钮
   - 关键词高亮通过 DOM TextNode 处理，避免对 HTML 字符串多次正则
   - 弹窗 a11y：aria-modal + 焦点陷阱 + 返回触发元素
   ============================================================ */

(function () {
  "use strict";

  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));

  // Cycle 20: SPA no longer reads the raw NCGA_AUTH_TOKEN from a <meta> tag.
  // Server sets an HMAC-signed HttpOnly cookie when serving /, and `fetch`
  // ships it automatically with same-origin requests. We just need to ensure
  // no code path overrides credentials. We also surface 401 → "session
  // expired" once so the user knows to refresh.
  const _AUTH_MODE =
    document.querySelector('meta[name="ncga-auth-mode"]')?.content || "open";
  const _origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    if (url.startsWith("/api/")) {
      init = init || {};
      if (!init.credentials) init.credentials = "same-origin";
    }
    return _origFetch(input, init).then((res) => {
      if (res && res.status === 401 && url.startsWith("/api/")) {
        const now = Date.now();
        if (!window.__ncga401At || now - window.__ncga401At > 5000) {
          window.__ncga401At = now;
          try {
            if (typeof showToast === "function") {
              showToast("会话已过期,刷新页面重新登录", "fa-arrow-rotate-left");
            }
          } catch (_) { /* showToast may not be defined yet */ }
        }
      }
      return res;
    });
  };

  // ---------------- DOM refs ----------------
  const form = $("#rewrite-form");
  const textInput = $("#text");
  const targetSelect = $("#target_variety");
  const resultEl = $("#result");
  const statusEl = $("#status");
  const warningEl = $("#warning");
  const submitButton = $("#submit-button");
  const submitLabel = submitButton.querySelector(".button-label");
  const copyButton = $("#copy-button");
  const clearButton = $("#clear-button");
  const speakButton = $("#speak-button");
  const downloadButton = $("#download-button");
  const charCountEl = $("#char-count");
  const compareToggle = $("#compare-toggle");
  const favoriteBtn = $("#favorite-button");
  const favoriteLabel = $("#favorite-label");
  const cmdbarRewrite = $("#cmdbar-rewrite");

  const varietyCard = $("#variety-card");
  const varietyTitle = $("#variety-title");
  const varietyBadge = $("#variety-badge");
  const varietyTrial = $("#variety-trial");
  const varietyScript = $("#variety-script");
  const varietyRegister = $("#variety-register");
  const varietyStyle = $("#variety-style");

  const resultStage = $("#result-stage");
  const resultStats = $("#result-stats");
  const resultCharCount = $("#result-char-count");
  const resultScript = $("#result-script");
  const resultElapsed = $("#result-elapsed");
  const resultKeywords = $("#result-keywords");

  const landmarkTag = $("#landmark-tag");
  const landmarkName = $("#landmark-name");
  const degradedTag = $("#degraded-tag");

  const toast = $("#toast");
  const toastText = $("#toast-text");

  const bootScreen = $("#boot-screen");
  const bgImageA = $("#bg-image");
  const bgImageB = $("#bg-image-next");
  let activeBg = bgImageA;
  let inactiveBg = bgImageB;

  const sidebarToggle = $("#sidebar-toggle");
  const mobileMenuBtn = $("#mobile-menu-btn");
  const sidebarBackdrop = $("#sidebar-backdrop");
  const navItems = $$(".nav-item");
  const pageTitle = $("#page-title");
  const pageSubtitle = $("#page-subtitle");
  const bgModePill = $("#bg-mode-pill");
  const bgModeLabel = $("#bg-mode-label");

  const atlasGrid = $("#atlas-grid");
  const historyList = $("#history-list");
  const historyEmpty = $("#history-empty");
  const historySearch = $("#history-search");
  const historyFavOnly = $("#history-favorites-only");
  const historyExport = $("#history-export");
  const historyClear = $("#history-clear");

  const settingsExportAll = $("#settings-export-all");
  const settingsReset = $("#settings-reset");

  const scenarioChipsContainer = $("#scenario-chips");

  const compareOtherButton = $("#compare-other-button");
  const comparePanel = $("#compare-panel");
  const compareClose = $("#compare-close");
  const compareChipsContainer = $("#compare-chips");
  const compareGrid = $("#compare-grid");

  const explainButton = $("#explain-button");
  const explainDrawer = $("#explain-drawer");
  const explainClose = $("#explain-close");
  const explainBody = $("#explain-body");

  const landmarkModal = $("#landmark-modal");
  const landmarkClose = $("#landmark-close");
  const landmarkModalTitle = $("#landmark-modal-title");
  const landmarkModalGrid = $("#landmark-modal-grid");
  const landmarkRotateBtn = $("#landmark-rotate-btn");
  const landmarkUseBtn = $("#landmark-use-btn");

  // ---------------- constants ----------------
  const SCRIPT_LABEL = { simplified: "简体", traditional: "繁體" };
  // Cycle 19 v2: 今日方言 promoted to slot 1 + home/default route. Shortcut 1 → daily,
  // 2 → workbench, 3 → batch, 4 → atlas, 5 → history, 6 → settings, 7 → about.
  const ROUTES = ["daily", "workbench", "batch", "atlas", "history", "settings", "about"];
  const HISTORY_MAX = 50;

  const STATUS_TEXT = {
    idle: '<i class="fas fa-info-circle" aria-hidden="true"></i> 选择方言风格并点击"立即重写"',
    loading: '<i class="fas fa-circle-notch" aria-hidden="true"></i> 正在揉成地道的味儿…',
    success: (variety, script) =>
      `<i class="fas fa-check-circle" aria-hidden="true"></i> 重写完成！风味：${escapeHtml(variety)} · 字形：${escapeHtml(script)}`,
    degraded: (variety) =>
      `<i class="fas fa-triangle-exclamation" aria-hidden="true"></i> 已降级到本地启发式重写（${escapeHtml(variety)}），效果远不如 LLM。`,
    error: (msg) => `<i class="fas fa-exclamation-circle" aria-hidden="true"></i> 出错了：${escapeHtml(msg)}`,
    rateLimited: '<i class="fas fa-gauge-high" aria-hidden="true"></i> 触发限流，请稍后再试。',
    copied: '<i class="fas fa-clipboard-check" aria-hidden="true"></i> 已复制到剪贴板',
    speaking: '<i class="fas fa-volume-up" aria-hidden="true"></i> 正在朗读…',
    spoken: '<i class="fas fa-check" aria-hidden="true"></i> 朗读结束',
    noSpeech: '<i class="fas fa-triangle-exclamation" aria-hidden="true"></i> 当前浏览器不支持语音朗读',
    noTtsForVariety: (label) =>
      `<i class="fas fa-volume-xmark" aria-hidden="true"></i> ${escapeHtml(label)} 暂不支持朗读（避免普通话误读）`,
    loadFail: '<i class="fas fa-triangle-exclamation" aria-hidden="true"></i> 方言列表加载失败，请刷新重试',
    empty: '<i class="fas fa-pen-to-square" aria-hidden="true"></i> 想说的话还没写呢～',
    notSelected: '<i class="fas fa-hand-pointer" aria-hidden="true"></i> 还没挑方言呢，选一个再来～',
    trialNotice: (label) =>
      `<i class="fas fa-flask" aria-hidden="true"></i> ${escapeHtml(label)} 是试用版，输出可能不够稳定`,
  };

  // ---------------- state ----------------
  // VARIETIES is the single source of truth, populated from /api/presets:
  //   { value, label, script, letter, trial, tts_lang, description_short,
  //     description_style, keywords[], landmarks[{url,name}] }
  const VARIETIES = {};
  let varietyOrder = [];

  // SCENARIOS populated from /api/scenarios:
  //   { value, label, description }
  const SCENARIOS = {};
  let scenarioOrder = [];
  const DEFAULT_SCENARIO = "friends_casual";

  let lastResult = null;
  let compareMode = false;
  let lastBgUrl = null;
  let bgRotateTimer = null;
  let currentBgIndex = 0;

  const DEFAULT_SETTINGS = {
    bgMode: "rotate",
    fontScale: "1",
    highlight: "on",
    version: "v2",
    pinnedLandmark: {},
    scenario: DEFAULT_SCENARIO,
  };
  let SETTINGS = loadSettings();

  // ---------------- storage helpers ----------------
  function loadSettings() {
    try {
      const raw = localStorage.getItem("ncga.settings.v2");
      if (!raw) return { ...DEFAULT_SETTINGS };
      const parsed = JSON.parse(raw);
      return { ...DEFAULT_SETTINGS, ...parsed, pinnedLandmark: parsed.pinnedLandmark || {} };
    } catch (_) { return { ...DEFAULT_SETTINGS }; }
  }
  function saveSettings() {
    try { localStorage.setItem("ncga.settings.v2", JSON.stringify(SETTINGS)); }
    catch (_) { /* quota exceeded — ignore */ }
  }

  function loadHistory() {
    try {
      const raw = localStorage.getItem("ncga.history.v2");
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch (_) { return []; }
  }
  function saveHistory(list) {
    try { localStorage.setItem("ncga.history.v2", JSON.stringify(list.slice(0, HISTORY_MAX))); }
    catch (_) { /* ignore */ }
  }

  // History dedup key: target + normalized original. Identical re-rewrites refresh ts/result rather than pile up.
  function historyKey(record) {
    return `${record.target_variety}::${(record.original || "").trim()}`;
  }
  function pushHistory(record) {
    const list = loadHistory();
    const key = historyKey(record);
    const existingIdx = list.findIndex((r) => historyKey(r) === key);
    if (existingIdx >= 0) {
      // Preserve favorite flag across re-rewrites of the same input.
      record.favorite = record.favorite || !!list[existingIdx].favorite;
      list.splice(existingIdx, 1);
    }
    list.unshift(record);
    saveHistory(list);
  }
  function updateHistoryById(id, mutator) {
    const list = loadHistory();
    const idx = list.findIndex((r) => r.id === id);
    if (idx < 0) return false;
    mutator(list[idx]);
    saveHistory(list);
    return true;
  }

  // ---------------- DOM helpers ----------------
  function showToast(msg, icon = "fa-circle-check") {
    if (!toast) return;
    toastText.textContent = msg;
    const i = toast.querySelector("i");
    if (i) i.className = `fas ${icon}`;
    toast.hidden = false;
    requestAnimationFrame(() => toast.classList.add("show"));
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      toast.classList.remove("show");
      setTimeout(() => (toast.hidden = true), 300);
    }, 2200);
  }

  function setStatus(html, type) {
    statusEl.classList.remove("success", "error");
    if (type) statusEl.classList.add(type);
    statusEl.innerHTML = html;
  }

  function setBusy(isBusy) {
    submitButton.disabled = isBusy;
    cmdbarRewrite.disabled = isBusy;
    if (submitLabel) submitLabel.textContent = isBusy ? "正在重写" : "立即重写";
    submitButton.classList.toggle("loading", isBusy);
  }

  function countChars(text) { return Array.from(text || "").length; }
  function formatElapsed(ms) {
    if (ms < 1000) return `${ms} ms`;
    return `${(ms / 1000).toFixed(2)} s`;
  }
  function formatTime(ts) {
    const d = new Date(ts);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  // ---------------- keyword highlight (DOM-based) ----------------
  // Walk the result <pre>'s text nodes and wrap matching keywords with <mark class="kw-hl">.
  // Far less brittle than HTML-string regex replace, and immune to substring collisions on tag names.
  function applyKeywordHighlight(container, varietyKey) {
    const v = VARIETIES[varietyKey];
    if (!v || !v.keywords || !v.keywords.length || SETTINGS.highlight !== "on") return;
    // Sort longest first so multi-char keywords win over single-char prefixes.
    const sorted = [...v.keywords].sort((a, b) => b.length - a.length);
    const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = new RegExp(sorted.map(escapeRe).join("|"), "g");

    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let node;
    while ((node = walker.nextNode())) nodes.push(node);

    nodes.forEach((textNode) => {
      const text = textNode.nodeValue;
      if (!pattern.test(text)) return;
      pattern.lastIndex = 0;
      const frag = document.createDocumentFragment();
      let lastIdx = 0;
      let m;
      while ((m = pattern.exec(text))) {
        if (m.index > lastIdx) {
          frag.appendChild(document.createTextNode(text.slice(lastIdx, m.index)));
        }
        const mark = document.createElement("mark");
        mark.className = "kw-hl";
        mark.textContent = m[0];
        frag.appendChild(mark);
        lastIdx = m.index + m[0].length;
      }
      if (lastIdx < text.length) frag.appendChild(document.createTextNode(text.slice(lastIdx)));
      textNode.parentNode.replaceChild(frag, textNode);
    });
  }

  function detectKeywords(text, varietyKey) {
    const v = VARIETIES[varietyKey];
    if (!v || !v.keywords) return [];
    const found = new Set();
    v.keywords.forEach((kw) => { if (text.includes(kw)) found.add(kw); });
    return Array.from(found);
  }

  // ---------------- background ----------------
  const FAILED_BG = new Set();

  function preloadImage(url) {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => resolve(true);
      img.onerror = () => resolve(false);
      img.src = url;
    });
  }

  // Cycle 9 fix: token-versioned background — when user switches variety, bgVersion bumps;
  // any in-flight preload from the previous variety detects its token is stale and bails.
  let bgVersion = 0;

  async function setBackgroundUrl(url, name, expectedVersion) {
    if (!url || url === lastBgUrl) return true;
    if (expectedVersion === undefined) expectedVersion = bgVersion;
    const ok = await preloadImage(url);
    // If our version is stale, the user has since switched varieties — don't paint.
    if (expectedVersion !== bgVersion) return false;
    if (!ok) {
      FAILED_BG.add(url);
      return false;
    }
    lastBgUrl = url;
    inactiveBg.style.backgroundImage = `url("${url}")`;
    void inactiveBg.offsetWidth;
    inactiveBg.classList.add("is-active");
    activeBg.classList.remove("is-active");
    [activeBg, inactiveBg] = [inactiveBg, activeBg];
    if (name) updateLandmarkTag(name);
    return true;
  }

  function getValidLandmarks(varietyKey) {
    const list = (VARIETIES[varietyKey] && VARIETIES[varietyKey].landmarks) || [];
    const filtered = list.filter((l) => !FAILED_BG.has(l.url));
    return filtered.length ? filtered : list;
  }

  function stopBgRotate() {
    if (bgRotateTimer) {
      clearInterval(bgRotateTimer);
      bgRotateTimer = null;
    }
  }

  function startBgRotate(varietyKey) {
    stopBgRotate();
    if (SETTINGS.bgMode !== "rotate") return;
    const list = getValidLandmarks(varietyKey);
    if (list.length <= 1) return;
    const myVersion = bgVersion;  // cycle 9: capture token; don't let stale rotations fire
    bgRotateTimer = setInterval(async () => {
      if (myVersion !== bgVersion) { stopBgRotate(); return; }
      for (let tried = 0; tried < list.length; tried++) {
        currentBgIndex = (currentBgIndex + 1) % list.length;
        const item = list[currentBgIndex];
        if (FAILED_BG.has(item.url)) continue;
        const ok = await setBackgroundUrl(item.url, item.name, myVersion);
        if (ok) break;
      }
    }, 12000);
  }

  function updateLandmarkTag(name) {
    if (!landmarkTag) return;
    if (!name) { landmarkTag.hidden = true; return; }
    landmarkTag.hidden = false;
    landmarkName.textContent = name;
  }

  function applyBackgroundFor(varietyKey) {
    bgVersion++;  // cycle 9: invalidate any in-flight preload from previous variety
    stopBgRotate();

    // v3「随四时」: hero always updates (independent of bgMode), pick the first landmark.
    const allLandmarks = (VARIETIES[varietyKey] && VARIETIES[varietyKey].landmarks) || [];
    if (allLandmarks.length) updateV3Hero(varietyKey, allLandmarks[0]);

    if (SETTINGS.bgMode === "off") {
      document.body.setAttribute("data-bg-mode", "off");
      updateLandmarkTag(null);
      return;
    }
    document.body.removeAttribute("data-bg-mode");

    const list = getValidLandmarks(varietyKey) || [];
    if (!list.length) return;

    const pinnedUrl = SETTINGS.pinnedLandmark[varietyKey];
    let pickIdx = 0;
    if (pinnedUrl) {
      pickIdx = list.findIndex((l) => l.url === pinnedUrl);
      if (pickIdx < 0) pickIdx = 0;
    }
    currentBgIndex = pickIdx;
    const first = list[pickIdx];
    setBackgroundUrl(first.url);
    updateLandmarkTag(first.name);

    if (SETTINGS.bgMode === "rotate") startBgRotate(varietyKey);
  }

  // v3「随四时」 Stage A1 — seasonal landmark dataset (40 entries, lazy-loaded).
  // Schema: SEASONAL_LANDMARKS[variety_key][season] = { url, landmark, location, caption }
  let SEASONAL_LANDMARKS = null;
  let SEASONAL_LANDMARKS_LOADING = false;

  async function loadSeasonalLandmarks() {
    if (SEASONAL_LANDMARKS || SEASONAL_LANDMARKS_LOADING) return SEASONAL_LANDMARKS;
    SEASONAL_LANDMARKS_LOADING = true;
    try {
      const res = await fetch("/static/seasonal_landmarks.json", { credentials: "same-origin" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      // Strip _meta envelope
      delete data._meta;
      SEASONAL_LANDMARKS = data;
      // Re-render hero if v3 is active and a variety is selected,
      // so the upgraded seasonal entry takes effect immediately.
      if (document.body.getAttribute("data-v3") === "on") {
        const sel = $("#variety");
        if (sel && sel.value) applyBackgroundFor(sel.value);
      }
    } catch (e) {
      // Silent — v3 hero falls back to presets.py first landmark.
      console.warn("seasonal_landmarks load failed", e);
      SEASONAL_LANDMARKS = {};  // empty cache, stop retrying
    } finally {
      SEASONAL_LANDMARKS_LOADING = false;
    }
    return SEASONAL_LANDMARKS;
  }

  function getSeasonalEntry(varietyKey, season) {
    if (!SEASONAL_LANDMARKS) return null;
    const byVariety = SEASONAL_LANDMARKS[varietyKey];
    if (!byVariety) return null;
    return byVariety[season] || null;
  }

  // v3「随四时」 Stage A7 — 16 首古诗,4 季 × 4 首。
  // 选诗策略:按"年内第几天 + 季节槽数"日级稳定,每天午夜换一句不闪。
  const SEASONAL_VERSES = {
    spring: [
      { verse: "二月春风似剪刀", source: "贺知章 · 咏柳" },
      { verse: "春眠不觉晓,处处闻啼鸟", source: "孟浩然 · 春晓" },
      { verse: "等闲识得东风面,万紫千红总是春", source: "朱熹 · 春日" },
      { verse: "沾衣欲湿杏花雨,吹面不寒杨柳风", source: "志南 · 绝句" },
    ],
    summer: [
      { verse: "接天莲叶无穷碧,映日荷花别样红", source: "杨万里 · 晓出净慈寺送林子方" },
      { verse: "黄梅时节家家雨,青草池塘处处蛙", source: "赵师秀 · 约客" },
      { verse: "小荷才露尖尖角,早有蜻蜓立上头", source: "杨万里 · 小池" },
      { verse: "卷地风来忽吹散,望湖楼下水如天", source: "苏轼 · 六月二十七日望湖楼醉书" },
    ],
    autumn: [
      { verse: "停车坐爱枫林晚,霜叶红于二月花", source: "杜牧 · 山行" },
      { verse: "自古逢秋悲寂寥,我言秋日胜春朝", source: "刘禹锡 · 秋词" },
      { verse: "月落乌啼霜满天,江枫渔火对愁眠", source: "张继 · 枫桥夜泊" },
      { verse: "空山新雨后,天气晚来秋", source: "王维 · 山居秋暝" },
    ],
    winter: [
      { verse: "千山鸟飞绝,万径人踪灭", source: "柳宗元 · 江雪" },
      { verse: "忽如一夜春风来,千树万树梨花开", source: "岑参 · 白雪歌送武判官归京" },
      { verse: "墙角数枝梅,凌寒独自开", source: "王安石 · 梅花" },
      { verse: "晚来天欲雪,能饮一杯无", source: "白居易 · 问刘十九" },
    ],
  };

  function pickVerseForSeason(season) {
    const list = SEASONAL_VERSES[season] || [];
    if (!list.length) return null;
    const dayIdx = Math.floor(Date.now() / (24 * 3600 * 1000)) % list.length;
    return list[dayIdx];
  }

  // R-A1: 24 节气表 — 按月日近似匹配,不准但意境到位
  // (精确节气需天文计算,这里用每月固定日 5/20 简化)
  const SOLAR_TERMS = [
    [2, 4, "立春"], [2, 19, "雨水"], [3, 6, "惊蛰"], [3, 21, "春分"],
    [4, 5, "清明"], [4, 20, "谷雨"],
    [5, 6, "立夏"], [5, 21, "小满"], [6, 6, "芒种"], [6, 21, "夏至"],
    [7, 7, "小暑"], [7, 23, "大暑"],
    [8, 8, "立秋"], [8, 23, "处暑"], [9, 8, "白露"], [9, 23, "秋分"],
    [10, 8, "寒露"], [10, 23, "霜降"],
    [11, 7, "立冬"], [11, 22, "小雪"], [12, 7, "大雪"], [12, 22, "冬至"],
    [1, 6, "小寒"], [1, 20, "大寒"],
  ];
  function currentSolarTerm() {
    const now = new Date();
    const m = now.getMonth() + 1, d = now.getDate();
    let best = "立春";
    for (const [tm, td, name] of SOLAR_TERMS) {
      // Pick the latest term whose start date <= today (in calendar order)
      if (tm < m || (tm === m && td <= d)) best = name;
      // Special wrap: if Jan and we passed 6/20, "小寒/大寒" handled above naturally
    }
    return best;
  }

  // v3「随四时」: render hero photo + caption based on current variety + season.
  // Prefers SEASONAL_LANDMARKS[variety][season] (Stage A1 dataset).
  // Falls back to the variety's first preset landmark if the dataset isn't
  // loaded yet or doesn't cover the variety.
  function updateV3Hero(varietyKey, landmark) {
    if (!varietyKey) return;
    const v = VARIETIES[varietyKey];
    if (!v) return;
    const season = currentSeason();

    // Resolve photo source: seasonal entry > caller-supplied landmark > nothing
    const seasonal = getSeasonalEntry(varietyKey, season);
    const photoUrl = (seasonal && seasonal.url) || (landmark && landmark.url) || null;
    const landmarkLabel = (seasonal && seasonal.landmark) || (landmark && landmark.name) || "—";
    const locationLabel = (seasonal && seasonal.location) || v.label || "";

    const photo = $("#v3-hero-photo");
    const metaPlace = $("#v3-hero-meta-place");
    const metaSolar = $("#v3-hero-meta-solar");
    const headline = $("#v3-hero-headline");
    if (photo && photoUrl) {
      photo.style.backgroundImage = `url("${photoUrl}")`;
    }
    if (metaPlace) metaPlace.textContent = locationLabel;
    if (metaSolar) metaSolar.textContent = currentSolarTerm();
    if (headline) headline.textContent = landmarkLabel;
    // Stage A7: render today's verse for the active season.
    const verseEl = $("#v3-hero-verse");
    if (verseEl) {
      const picked = pickVerseForSeason(season);
      if (picked) {
        // Use DOM-API style sets (Cycle 20 CSP: no inline styles or innerHTML).
        verseEl.textContent = "";
        const q = document.createElement("span");
        q.textContent = "「" + picked.verse + "」";
        const dash = document.createElement("span");
        dash.className = "v3-hero-verse-sep";
        dash.textContent = "  — ";
        const src = document.createElement("span");
        src.className = "v3-hero-verse-source";
        src.textContent = picked.source;
        verseEl.appendChild(q);
        verseEl.appendChild(dash);
        verseEl.appendChild(src);
      } else {
        verseEl.textContent = "「随四时,听乡音」";
      }
    }
  }

  // v3 mode opt-in: URL ?v3=1 (one-time toggle, persists to localStorage),
  // ?v3=0 to opt out, otherwise read localStorage flag. Stage A7 will flip
  // this to default-on; for now Stage A4-A6 are preview-only.
  function detectV3Mode() {
    try {
      const q = (location.search || "") + (location.hash || "");
      if (q.indexOf("v3=1") !== -1) localStorage.setItem("ncga.v3", "1");
      if (q.indexOf("v3=0") !== -1) localStorage.removeItem("ncga.v3");
      if (localStorage.getItem("ncga.v3") === "1") {
        document.body.setAttribute("data-v3", "on");
        const hero = $("#v3-hero");
        if (hero) hero.setAttribute("aria-hidden", "false");
        // R-A3: inject 8 petal divs into deco layer (CSS handles animation per :nth-child)
        injectSeasonDeco();
      }
    } catch (e) { /* ignore — preview flag silently disabled */ }
  }

  // R-A3: 春日装饰 — 在 .v3-season-deco 里注入 8 个 .v3-petal,CSS 飘落动画。
  // 夏/秋/冬下个 cycle 加(同一容器换 class:.v3-firefly / .v3-mapleleaf / .v3-snowflake)
  function injectSeasonDeco() {
    const deco = $("#v3-season-deco");
    if (!deco) return;
    deco.textContent = "";          // 清旧
    const season = currentSeason();
    if (season === "spring") {
      for (let i = 0; i < 8; i++) {
        const p = document.createElement("div");
        p.className = "v3-petal";
        deco.appendChild(p);
      }
    }
    // 夏/秋/冬留待下次 cycle:占位结构已就绪
  }

  function updateBgModePill() {
    const mode = SETTINGS.bgMode;
    const map = { rotate: "轮播", static: "固定", off: "关闭" };
    const iconMap = { rotate: "fa-images", static: "fa-thumbtack", off: "fa-eye-slash" };
    bgModeLabel.textContent = map[mode] || "—";
    const iconEl = bgModePill.querySelector("i");
    if (iconEl) iconEl.className = `fas ${iconMap[mode] || "fa-image"}`;
  }

  bgModePill.addEventListener("click", () => {
    const order = ["rotate", "static", "off"];
    const idx = order.indexOf(SETTINGS.bgMode);
    SETTINGS.bgMode = order[(idx + 1) % order.length];
    saveSettings();
    syncSettingsUI();
    updateBgModePill();
    applyBackgroundFor(targetSelect.value || varietyOrder[0]);
    showToast(`背景模式：${ { rotate: "轮播", static: "固定", off: "关闭" }[SETTINGS.bgMode] }`,
      { rotate: "fa-images", static: "fa-thumbtack", off: "fa-eye-slash" }[SETTINGS.bgMode]);
  });

  // ---------------- routing ----------------
  function gotoRoute(route) {
    if (!ROUTES.includes(route)) route = "workbench";
    document.body.setAttribute("data-route", route);
    $$(".page").forEach((p) => p.classList.remove("is-active"));
    const page = $(`#page-${route}`);
    if (page) page.classList.add("is-active");
    navItems.forEach((n) => {
      const active = n.dataset.route === route;
      n.classList.toggle("is-active", active);
      n.setAttribute("aria-selected", active ? "true" : "false");
    });

    const titles = {
      // Cycle 19 v2: 'daily' added — without this entry, t[0] threw on undefined
      // and crashed the whole init IIFE (boot screen never hid, data never loaded).
      daily: ["今日方言一句", "同一句智慧 · 十种乡音说出来"],
      workbench: ["重写台", "把任意中文揉成你想要的乡音"],
      batch: ["批量工作台", "一次跑完几十条 × 多方言"],
      atlas: ["方言图鉴", "看看每种方言背后的地标与文化"],
      history: ["历史记录", "本地保留最近 50 条重写记录"],
      settings: ["设置", "调整背景、字号、高亮等偏好"],
      about: ["关于", "关于这只小助手 · 写作贴士 · 快捷键"],
    };
    const t = titles[route] || titles.workbench;
    pageTitle.textContent = t[0];
    pageSubtitle.textContent = t[1];

    if (route === "history") renderHistory();
    if (route === "atlas") renderAtlas();
    if (route === "batch") renderBatchInputs();
    if (route === "settings") loadQualityStats();

    document.body.classList.remove("sidebar-open");
  }

  function bindRouter() {
    navItems.forEach((n) => {
      n.addEventListener("click", (e) => {
        e.preventDefault();
        location.hash = n.dataset.route;
      });
    });
    window.addEventListener("hashchange", () => {
      gotoRoute(location.hash.replace("#", ""));
    });
    // Cycle 19 v2: default landing route is now "daily" (was "workbench").
    gotoRoute(location.hash.replace("#", "") || "daily");
  }

  // ---------------- settings ----------------
  // v3「随四时」: detect season from current month (北半球节气近似).
  // 3-5 = 春 spring · 6-8 = 夏 summer · 9-11 = 秋 autumn · 12,1,2 = 冬 winter.
  // Override via localStorage `ncga.seasonOverride` ∈ {spring,summer,autumn,winter}
  // for demo / screenshot purposes.
  function currentSeason() {
    try {
      var override = localStorage.getItem("ncga.seasonOverride");
      if (override && /^(spring|summer|autumn|winter)$/.test(override)) {
        return override;
      }
    } catch (e) { /* localStorage may be blocked — fall through */ }
    var m = new Date().getMonth() + 1;  // 1..12
    if (m >= 3 && m <= 5) return "spring";
    if (m >= 6 && m <= 8) return "summer";
    if (m >= 9 && m <= 11) return "autumn";
    return "winter";
  }

  function applySeason(season) {
    document.documentElement.setAttribute("data-season", season);
    document.body.setAttribute("data-season", season);
  }

  function applyVersion(v) {
    document.documentElement.setAttribute("data-version", v);
    document.body.setAttribute("data-version", v);
    SETTINGS.version = v;
    $$(".version-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.version === v));
  }
  function applyFontScale(scale) {
    document.documentElement.style.setProperty("--font-scale", scale);
  }
  function syncSettingsUI() {
    document.body.setAttribute("data-version", SETTINGS.version);
    document.documentElement.setAttribute("data-version", SETTINGS.version);
    $$(".version-btn").forEach((b) => b.classList.toggle("is-active", b.dataset.version === SETTINGS.version));
    $$(".seg-control").forEach((wrap) => {
      const key = wrap.dataset.setting;
      $$("button", wrap).forEach((btn) =>
        btn.classList.toggle("is-active", String(SETTINGS[key]) === btn.dataset.value)
      );
    });
    applyFontScale(SETTINGS.fontScale);
  }

  function bindSettings() {
    $$(".seg-control").forEach((wrap) => {
      const key = wrap.dataset.setting;
      $$("button", wrap).forEach((btn) => {
        btn.addEventListener("click", () => {
          $$("button", wrap).forEach((b) => b.classList.remove("is-active"));
          btn.classList.add("is-active");
          SETTINGS[key] = btn.dataset.value;
          saveSettings();
          if (key === "version") applyVersion(SETTINGS.version);
          if (key === "fontScale") applyFontScale(SETTINGS.fontScale);
          if (key === "bgMode") {
            updateBgModePill();
            applyBackgroundFor(targetSelect.value || varietyOrder[0]);
          }
          if (key === "highlight" && lastResult) {
            renderResult(lastResult.rewritten_text, lastResult.target_variety);
          }
        });
      });
    });
    $$(".version-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        SETTINGS.version = btn.dataset.version;
        saveSettings();
        applyVersion(SETTINGS.version);
        syncSettingsUI();
        showToast(SETTINGS.version === "v2" ? "已切换到「墨韵」版" : "已切换到「古朴」版", "fa-paintbrush");
      });
    });

    settingsExportAll.addEventListener("click", () => {
      const blob = new Blob(
        [JSON.stringify({ settings: SETTINGS, history: loadHistory() }, null, 2)],
        { type: "application/json" }
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `地道中文_数据_${Date.now()}.json`;
      a.click();
      URL.revokeObjectURL(url);
      showToast("已导出全部数据", "fa-file-export");
    });

    settingsReset.addEventListener("click", () => {
      if (!confirm("确认重置所有偏好设置？此操作不会删除历史记录。")) return;
      SETTINGS = { ...DEFAULT_SETTINGS };
      saveSettings();
      syncSettingsUI();
      applyVersion(SETTINGS.version);
      applyFontScale(SETTINGS.fontScale);
      updateBgModePill();
      applyBackgroundFor(targetSelect.value || varietyOrder[0]);
      renderScenarioChips();
      showToast("已重置偏好", "fa-rotate-left");
    });
  }

  // ---------------- history ----------------
  function renderHistory(filter = "") {
    const list = loadHistory();
    const q = (filter || historySearch.value || "").trim().toLowerCase();
    const favOnly = !!historyFavOnly.checked;
    const filtered = list.filter((r) => {
      if (favOnly && !r.favorite) return false;
      if (!q) return true;
      const blob = `${r.original} ${r.rewritten} ${r.target_variety} ${r.label}`.toLowerCase();
      return blob.includes(q);
    });

    if (!filtered.length) {
      historyList.innerHTML = "";
      historyEmpty.hidden = false;
      return;
    }
    historyEmpty.hidden = true;
    historyList.innerHTML = filtered.map((r) => `
      <article class="history-item${r.favorite ? " is-favorite" : ""}${r.degraded ? " is-degraded" : ""}" data-id="${escapeHtml(r.id)}">
        <div class="history-meta">
          <span class="history-variety">${escapeHtml(r.label)}</span>
          ${r.degraded ? '<span class="history-flag">已降级</span>' : ""}
          ${r.favorite ? '<span class="history-fav"><i class="fas fa-star" aria-hidden="true"></i></span>' : ""}
          <span class="history-time">${formatTime(r.ts)}</span>
        </div>
        <div class="history-body">
          <div class="history-orig">${escapeHtml(r.original)}</div>
          <div class="history-result">${escapeHtml(r.rewritten)}</div>
        </div>
        <div class="history-actions">
          <button class="icon-btn" data-act="favorite" title="${r.favorite ? "取消收藏" : "收藏"}" aria-pressed="${r.favorite ? "true" : "false"}">
            <i class="${r.favorite ? "fas" : "far"} fa-star" aria-hidden="true"></i>
          </button>
          <button class="icon-btn" data-act="reuse" title="把这条放回重写台">
            <i class="fas fa-arrow-rotate-left" aria-hidden="true"></i>
          </button>
          <button class="icon-btn" data-act="copy" title="复制结果">
            <i class="far fa-copy" aria-hidden="true"></i>
          </button>
          <button class="icon-btn" data-act="delete" title="删除">
            <i class="fas fa-trash" aria-hidden="true"></i>
          </button>
        </div>
      </article>
    `).join("");

    $$(".history-item", historyList).forEach((node) => {
      const id = node.dataset.id;
      const item = filtered.find((x) => x.id === id);
      if (!item) return;
      node.querySelector('[data-act="copy"]').addEventListener("click", async () => {
        await navigator.clipboard.writeText(item.rewritten);
        showToast("已复制结果", "fa-clipboard-check");
      });
      node.querySelector('[data-act="reuse"]').addEventListener("click", () => {
        // Cycle 18: history "放回" used to clobber a draft in the workbench.
        // Confirm if the user has unsaved content that's not just the same item.
        const current = (textInput.value || "").trim();
        if (current && current !== item.original.trim()) {
          const ok = window.confirm(
            `重写台里有内容会被替换：\n\n当前：${current.slice(0, 40)}${current.length > 40 ? "…" : ""}\n\n要换成这条历史的原文吗？`
          );
          if (!ok) return;
        }
        textInput.value = item.original;
        targetSelect.value = item.target_variety;
        updateCharCount();
        updateVarietyCard();
        applyBackgroundFor(item.target_variety);
        location.hash = "workbench";
        showToast("已放回重写台", "fa-arrow-rotate-left");
      });
      node.querySelector('[data-act="delete"]').addEventListener("click", () => {
        const all = loadHistory();
        const idx = all.findIndex((x) => x.id === id);
        if (idx >= 0) all.splice(idx, 1);
        saveHistory(all);
        renderHistory();
        showToast("已删除", "fa-trash");
      });
      node.querySelector('[data-act="favorite"]').addEventListener("click", () => {
        const ok = updateHistoryById(id, (r) => { r.favorite = !r.favorite; });
        if (!ok) return;
        renderHistory();
        // Sync the workbench star if this is the current result.
        if (lastResult && lastResult.id === id) {
          syncFavoriteButton(loadHistory().find((x) => x.id === id));
        }
      });
    });
  }

  function bindHistoryTools() {
    historySearch.addEventListener("input", () => renderHistory());
    historyFavOnly.addEventListener("change", () => renderHistory());
    historyExport.addEventListener("click", () => {
      const all = loadHistory();
      if (!all.length) { showToast("还没有历史可导出", "fa-circle-info"); return; }
      const blob = new Blob([JSON.stringify(all, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `地道中文_历史_${Date.now()}.json`; a.click();
      URL.revokeObjectURL(url);
      showToast("已导出历史", "fa-file-export");
    });
    historyClear.addEventListener("click", () => {
      if (!confirm("确认清空所有历史记录？此操作不可撤销。")) return;
      saveHistory([]);
      renderHistory();
      showToast("历史已清空", "fa-trash");
    });
  }

  // ---------------- atlas ----------------
  function renderAtlas() {
    if (!varietyOrder.length) return;
    // Cycle 20: HTML emits `data-bg` instead of inline `style=` so we can
    // drop CSP `style-src 'unsafe-inline'`. Background image is set via
    // DOM API after insertion — CSP doesn't gate element.style.foo writes.
    atlasGrid.innerHTML = varietyOrder.map((key) => {
      const v = VARIETIES[key];
      const list = v.landmarks || [];
      const cover = (SETTINGS.pinnedLandmark[key] && list.find((l) => l.url === SETTINGS.pinnedLandmark[key])) || list[0] || {};
      return `
        <article class="variety-tile" data-variety="${escapeHtml(key)}">
          <div class="variety-thumb" data-bg="${escapeHtml(cover.url || "")}">
            <span class="variety-letter">${escapeHtml(v.letter || "")}</span>
            <span class="variety-landmark-name">${escapeHtml(cover.name || "")}</span>
            ${v.trial ? '<span class="trial-flag"><i class="fas fa-flask" aria-hidden="true"></i> 试用版</span>' : ""}
          </div>
          <div class="variety-body">
            <h3>${escapeHtml(v.label)}</h3>
            <p>${escapeHtml(v.description_style || "")}</p>
            <div class="variety-actions">
              <button class="ghost-btn" data-act="pick" title="选择地标 / 设为固定背景">
                <i class="fas fa-images" aria-hidden="true"></i> ${list.length} 张地标
              </button>
              <button class="primary-btn" data-act="use">
                <i class="fas fa-feather-pointed" aria-hidden="true"></i> 用它重写
              </button>
            </div>
          </div>
        </article>
      `;
    }).join("");
    $$(".variety-thumb[data-bg]", atlasGrid).forEach((el) => {
      const url = el.dataset.bg;
      if (url) el.style.backgroundImage = `url('${url}')`;
    });

    $$(".variety-tile", atlasGrid).forEach((tile) => {
      const v = tile.dataset.variety;
      tile.querySelector('[data-act="pick"]').addEventListener("click", (e) => {
        e.stopPropagation();
        openLandmarkModal(v, tile.querySelector('[data-act="pick"]'));
      });
      tile.querySelector('[data-act="use"]').addEventListener("click", (e) => {
        e.stopPropagation();
        useVariety(v);
      });
      tile.addEventListener("click", () => useVariety(v));
    });
  }

  function useVariety(v) {
    if (!targetSelect.querySelector(`option[value="${CSS.escape(v)}"]`)) return;
    targetSelect.value = v;
    updateVarietyCard();
    applyBackgroundFor(v);
    if (VARIETIES[v] && VARIETIES[v].trial) {
      showToast(`${VARIETIES[v].label}：试用版，输出可能波动`, "fa-flask");
    }
    location.hash = "workbench";
    setTimeout(() => textInput.focus(), 350);
  }

  // ---------------- landmark modal (a11y) ----------------
  let modalCurrentVariety = null;
  let modalCurrentPick = null;
  let modalReturnFocus = null;
  let modalKeydownHandler = null;

  function getFocusable(root) {
    return $$(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      root
    ).filter((el) => !el.disabled && el.offsetParent !== null);
  }

  function openLandmarkModal(varietyKey, returnFocusEl) {
    modalCurrentVariety = varietyKey;
    const v = VARIETIES[varietyKey];
    const list = (v && v.landmarks) || [];
    modalCurrentPick = SETTINGS.pinnedLandmark[varietyKey] || (list[0] && list[0].url);
    landmarkModalTitle.textContent = `${(v && v.label) || varietyKey} · 地标选择`;
    // Cycle 20: no inline style= on the HTML → CSP-clean; set via DOM API.
    landmarkModalGrid.innerHTML = list.map((l) => `
      <button type="button"
              class="landmark-pick ${l.url === modalCurrentPick ? "is-selected" : ""}"
              data-url="${escapeHtml(l.url)}"
              aria-pressed="${l.url === modalCurrentPick ? "true" : "false"}">
        <span class="pick-name">${escapeHtml(l.name)}</span>
      </button>
    `).join("");
    $$(".landmark-pick", landmarkModalGrid).forEach((p) => {
      if (p.dataset.url) p.style.backgroundImage = `url('${p.dataset.url}')`;
      p.addEventListener("click", () => {
        modalCurrentPick = p.dataset.url;
        $$(".landmark-pick", landmarkModalGrid).forEach((x) => {
          const sel = x.dataset.url === modalCurrentPick;
          x.classList.toggle("is-selected", sel);
          x.setAttribute("aria-pressed", sel ? "true" : "false");
        });
      });
    });
    landmarkModal.hidden = false;

    modalReturnFocus = returnFocusEl || document.activeElement;
    // Focus first interactive element
    const focusables = getFocusable(landmarkModal);
    if (focusables[0]) focusables[0].focus();

    modalKeydownHandler = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeLandmarkModal();
        return;
      }
      if (e.key !== "Tab") return;
      const f = getFocusable(landmarkModal);
      if (!f.length) return;
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", modalKeydownHandler);
  }

  function closeLandmarkModal() {
    landmarkModal.hidden = true;
    if (modalKeydownHandler) {
      document.removeEventListener("keydown", modalKeydownHandler);
      modalKeydownHandler = null;
    }
    if (modalReturnFocus && modalReturnFocus.focus) {
      modalReturnFocus.focus();
    }
    modalReturnFocus = null;
  }

  landmarkClose.addEventListener("click", closeLandmarkModal);
  landmarkModal.addEventListener("click", (e) => { if (e.target === landmarkModal) closeLandmarkModal(); });

  landmarkRotateBtn.addEventListener("click", () => {
    if (!modalCurrentVariety) return;
    delete SETTINGS.pinnedLandmark[modalCurrentVariety];
    SETTINGS.bgMode = "rotate";
    saveSettings();
    syncSettingsUI();
    updateBgModePill();
    applyBackgroundFor(modalCurrentVariety);
    closeLandmarkModal();
    showToast("已设为该方言轮播", "fa-images");
  });

  landmarkUseBtn.addEventListener("click", () => {
    if (!modalCurrentVariety || !modalCurrentPick) return;
    SETTINGS.pinnedLandmark[modalCurrentVariety] = modalCurrentPick;
    SETTINGS.bgMode = "static";
    saveSettings();
    syncSettingsUI();
    updateBgModePill();
    targetSelect.value = modalCurrentVariety;
    updateVarietyCard();
    applyBackgroundFor(modalCurrentVariety);
    closeLandmarkModal();
    location.hash = "workbench";
    showToast("已固定为该地标", "fa-thumbtack");
  });

  // ---------------- workbench ----------------
  function updateCharCount() {
    const length = textInput.value.length;
    charCountEl.textContent = length;
    if (length > 1000) charCountEl.style.color = "var(--warning)";
    else if (length > 800) charCountEl.style.color = "#d18b2a";
    else charCountEl.style.color = "";
  }

  function updateVarietyCard() {
    const value = targetSelect.value;
    const v = VARIETIES[value];
    if (!v) { varietyCard.hidden = true; return; }
    varietyCard.hidden = false;
    varietyTitle.textContent = v.label;
    varietyBadge.textContent = "当前风味";
    varietyScript.textContent = SCRIPT_LABEL[v.script] || v.script;
    varietyRegister.textContent = v.description_short || "—";
    varietyStyle.textContent = v.description_style || "";
    if (varietyTrial) varietyTrial.hidden = !v.trial;

    // TTS gating
    const ttsAvailable = !!v.tts_lang && ("speechSynthesis" in window);
    speakButton.disabled = !ttsAvailable || !lastResult;
    speakButton.title = ttsAvailable
      ? "朗读结果"
      : (v.tts_lang ? "当前浏览器不支持语音朗读" : `${v.label} 暂不支持朗读`);
  }

  function renderResult(text, varietyKey) {
    resultEl.textContent = text;
    applyKeywordHighlight(resultEl, varietyKey);
    const kws = detectKeywords(text, varietyKey);
    if (resultKeywords) resultKeywords.textContent = kws.length ? kws.slice(0, 5).join("·") : "—";
  }

  function toggleCompare() {
    compareMode = !compareMode;
    compareToggle.setAttribute("aria-pressed", compareMode ? "true" : "false");
    if (compareMode) {
      resultStage.classList.add("compare");
      let orig = resultStage.querySelector(".result-compare-orig");
      if (!orig) {
        orig = document.createElement("pre");
        orig.className = "result-compare-orig";
        resultStage.insertBefore(orig, resultEl);
      }
      orig.textContent = textInput.value || "（原文为空）";
    } else {
      resultStage.classList.remove("compare");
      const orig = resultStage.querySelector(".result-compare-orig");
      if (orig) orig.remove();
    }
    compareToggle.classList.toggle("primary-icon", compareMode);
  }

  function clearAll() {
    // Cycle 18: don't silently nuke a draft. If there's input or a result, confirm.
    const hasInput = (textInput.value || "").trim().length > 0;
    const hasResult = (resultEl.textContent || "").trim().length > 0;
    if (hasInput || hasResult) {
      const ok = window.confirm("确认清空原文与结果？");
      if (!ok) return;
    }
    textInput.value = "";
    resultEl.textContent = "";
    setStatus(STATUS_TEXT.idle);
    warningEl.textContent = "";
    copyButton.disabled = true;
    speakButton.disabled = true;
    downloadButton.disabled = true;
    favoriteBtn.disabled = true;
    favoriteBtn.classList.remove("is-favorite");
    favoriteBtn.setAttribute("aria-pressed", "false");
    if (favoriteLabel) favoriteLabel.textContent = "收藏";
    favoriteBtn.querySelector("i").className = "far fa-star";
    if (explainButton) explainButton.disabled = true;
    if (compareOtherButton) compareOtherButton.disabled = true;
    closeCompare();
    closeExplain();
    resultStats.hidden = true;
    if (degradedTag) degradedTag.hidden = true;
    lastResult = null;
    if (compareMode) toggleCompare();
    updateCharCount();
  }

  function speakResult() {
    const text = resultEl.textContent;
    if (!text) return;
    if (!("speechSynthesis" in window)) { setStatus(STATUS_TEXT.noSpeech, "error"); return; }
    const v = VARIETIES[targetSelect.value];
    if (!v || !v.tts_lang) {
      setStatus(STATUS_TEXT.noTtsForVariety(v ? v.label : targetSelect.value), "error");
      return;
    }
    speechSynthesis.cancel();
    const utt = new SpeechSynthesisUtterance(text);
    utt.lang = v.tts_lang;
    const voices = speechSynthesis.getVoices();
    const preferred = voices.find((vv) => vv.lang === utt.lang) || voices.find((vv) => vv.lang && vv.lang.startsWith("zh"));
    if (preferred) utt.voice = preferred;
    speechSynthesis.speak(utt);
    setStatus(STATUS_TEXT.speaking);
    utt.onend = () => setStatus(STATUS_TEXT.spoken, "success");
    utt.onerror = () => setStatus(STATUS_TEXT.noSpeech, "error");
  }

  function downloadResult() {
    const text = resultEl.textContent;
    if (!text) return;
    const v = targetSelect.value || "rewritten";
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `地道中文_${v}_${Date.now()}.txt`;
    a.click();
    URL.revokeObjectURL(url);
    showToast("已导出为文本文件", "fa-file-arrow-down");
  }

  async function copyResult() {
    const text = resultEl.textContent;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setStatus(STATUS_TEXT.copied, "success");
      showToast("已复制到剪贴板", "fa-clipboard-check");
    } catch (_) {
      const range = document.createRange();
      range.selectNodeContents(resultEl);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      document.execCommand("copy");
      sel.removeAllRanges();
      showToast("已复制到剪贴板", "fa-clipboard-check");
    }
  }

  function syncFavoriteButton(record) {
    const fav = !!(record && record.favorite);
    favoriteBtn.classList.toggle("is-favorite", fav);
    favoriteBtn.setAttribute("aria-pressed", fav ? "true" : "false");
    if (favoriteLabel) favoriteLabel.textContent = fav ? "已收藏" : "收藏";
    favoriteBtn.querySelector("i").className = fav ? "fas fa-star" : "far fa-star";
  }

  async function loadPresets() {
    const response = await fetch("/api/presets");
    if (!response.ok) throw new Error("Failed to load presets");
    const data = await response.json();
    const presets = data.presets || [];
    varietyOrder = presets.map((p) => p.value);
    presets.forEach((p) => {
      VARIETIES[p.value] = p;
      const opt = document.createElement("option");
      opt.value = p.value;
      const trialTag = p.trial ? " · 试用" : "";
      const scriptLabel = SCRIPT_LABEL[p.script] || p.script;
      opt.textContent = `${p.label}（${scriptLabel}${trialTag}）`;
      targetSelect.appendChild(opt);
    });
  }

  async function loadScenarios() {
    let data;
    try {
      const response = await fetch("/api/scenarios");
      if (!response.ok) throw new Error("scenarios http " + response.status);
      data = await response.json();
    } catch (err) {
      // Don't block the UI if scenarios fail; fall back to default.
      console.warn("Failed to load scenarios:", err);
      return;
    }
    const list = data.scenarios || [];
    scenarioOrder = list.map((s) => s.value);
    list.forEach((s) => { SCENARIOS[s.value] = s; });
    renderScenarioChips();
  }

  function renderScenarioChips() {
    if (!scenarioChipsContainer || !scenarioOrder.length) return;
    const current = SETTINGS.scenario || DEFAULT_SCENARIO;
    scenarioChipsContainer.innerHTML = scenarioOrder.map((v) => {
      const s = SCENARIOS[v];
      const active = v === current;
      return `<button type="button"
                      class="scenario-chip${active ? " is-active" : ""}"
                      role="radio"
                      aria-checked="${active ? "true" : "false"}"
                      data-scenario="${escapeHtml(v)}"
                      title="${escapeHtml(s.description)}">${escapeHtml(s.label)}</button>`;
    }).join("");
    $$(".scenario-chip", scenarioChipsContainer).forEach((btn) => {
      btn.addEventListener("click", () => {
        const v = btn.dataset.scenario;
        SETTINGS.scenario = v;
        saveSettings();
        $$(".scenario-chip", scenarioChipsContainer).forEach((b) => {
          const a = b.dataset.scenario === v;
          b.classList.toggle("is-active", a);
          b.setAttribute("aria-checked", a ? "true" : "false");
        });
        showToast(`场景：${SCENARIOS[v].label}`, "fa-comments");
      });
    });
  }

  let rewriteAbortController = null; // module-level in-flight guard

  // Cycle 11: Stream consumer for /api/rewrite-stream. Returns a final-result object
  // {rewritten_text, target_variety, ...} on success, OR null on any error (caller
  // should fall back to /api/rewrite).
  async function tryStreamRewrite(body, varietyKey, signal) {
    let res;
    try {
      res = await fetch("/api/rewrite-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify(body),
        signal,
      });
    } catch (err) {
      if (err.name === "AbortError") throw err;
      return null;  // network error — fall back
    }
    if (!res.ok || !res.body) return null;
    const ct = res.headers.get("Content-Type") || "";
    if (!ct.includes("text/event-stream")) {
      return null;  // unexpected — fall back
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let lastPartial = "";
    let finalResult = null;
    let streamError = null;

    // Show that we're streaming (status update + clear result + cursor class)
    resultEl.textContent = "";
    resultEl.classList.add("is-streaming");
    setStatus('<i class="fas fa-circle-notch fa-spin" aria-hidden="true"></i> 正在生成…');

    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE events are separated by blank lines. Per WHATWG: \n\n delimits events.
        let sepIdx;
        while ((sepIdx = buffer.indexOf("\n\n")) !== -1) {
          const eventBlock = buffer.slice(0, sepIdx);
          buffer = buffer.slice(sepIdx + 2);
          const evt = parseSseEvent(eventBlock);
          if (!evt) continue;
          if (evt.event === "chunk") {
            const partial = evt.data?.partial ?? "";
            if (partial && partial !== lastPartial) {
              lastPartial = partial;
              // Progressive render
              renderResult(partial, varietyKey);
            }
          } else if (evt.event === "done") {
            finalResult = evt.data;
          } else if (evt.event === "error") {
            streamError = evt.data?.error || "stream error";
          }
        }
      }
    } catch (err) {
      resultEl.classList.remove("is-streaming");
      if (err.name === "AbortError") throw err;
      return null;
    }

    resultEl.classList.remove("is-streaming");
    if (streamError) return null;  // server-side error mid-stream — caller falls back
    if (!finalResult) return null;
    // Normalize to the same shape as /api/rewrite returns. The stream's done event
    // carries `rewritten_text`, `target_variety`, and (Cycle 16) `effective_scenario`.
    return {
      rewritten_text: finalResult.rewritten_text || lastPartial,
      target_variety: finalResult.target_variety || varietyKey,
      effective_scenario: finalResult.effective_scenario,
      script: VARIETIES[varietyKey]?.script,
      degraded: false,
      _from_stream: true,
    };
  }

  function parseSseEvent(block) {
    // Block is one event: lines like "event: chunk" / "data: {...}".
    // Per spec, multiple "data:" lines join with \n. event defaults to "message".
    let event = "message";
    const dataLines = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return null;
    const dataStr = dataLines.join("\n");
    let data = null;
    try { data = JSON.parse(dataStr); } catch (_) { data = dataStr; }
    return { event, data };
  }

  async function rewriteText(event) {
    if (event && event.preventDefault) event.preventDefault();

    if (!textInput.value.trim()) { setStatus(STATUS_TEXT.empty, "error"); textInput.focus(); return; }
    if (!targetSelect.value) { setStatus(STATUS_TEXT.notSelected, "error"); targetSelect.focus(); return; }

    // Cycle 9 fix: in-flight guard. Cancel any previous request before starting a new one.
    // This blocks Cmd+Enter spam from firing N parallel LLM calls.
    if (rewriteAbortController) rewriteAbortController.abort();
    rewriteAbortController = new AbortController();
    const myController = rewriteAbortController;

    // Cycle 9 fix: snapshot every dependency at start so a mid-flight variety/scenario change
    // doesn't corrupt the response binding.
    const frozenTarget = targetSelect.value;
    const frozenScenario = SETTINGS.scenario || DEFAULT_SCENARIO;
    const frozenVariety = VARIETIES[frozenTarget] || {};

    setStatus(STATUS_TEXT.loading);
    warningEl.textContent = "";
    resultEl.textContent = "";
    copyButton.disabled = true;
    speakButton.disabled = true;
    downloadButton.disabled = true;
    favoriteBtn.disabled = true;
    resultStats.hidden = true;
    if (degradedTag) degradedTag.hidden = true;
    setBusy(true);

    const startedAt = performance.now();
    const originalText = textInput.value;

    try {
      // Cycle 11: try streaming first; fall back to /api/rewrite on any error.
      // This gives users a "字一个个跳出来"的 UX while keeping a safety net.
      let data;
      const streamResult = await tryStreamRewrite({
        text: originalText,
        target_variety: frozenTarget,
        scenario: frozenScenario,
        glossary: getGlossaryLines(),
      }, frozenTarget, myController.signal);
      if (streamResult) {
        data = streamResult;
      } else {
        // Fallback to non-streaming endpoint
        const res = await fetch("/api/rewrite", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            text: originalText,
            target_variety: frozenTarget,
            scenario: frozenScenario,
            glossary: getGlossaryLines(),
          }),
          signal: myController.signal,
        });
        if (res.status === 429) {
          setStatus(STATUS_TEXT.rateLimited, "error");
          return;
        }
        try { data = await res.json(); } catch (_) { data = {}; }
        if (!res.ok) throw new Error(data.error || `重写失败 (HTTP ${res.status})`);
      }

      // Stale-binding guard: if user fired a newer rewrite while we were waiting,
      // myController is no longer the active one — bail without mutating UI.
      if (rewriteAbortController !== myController) return;

      // Cycle 17: surface silent scenario fallback. Backend defaults unknown
      // scenarios to friends_casual silently; if that happens (stale frontend
      // cache, typo'd value, future scenario removed), tell the user with a toast.
      if (frozenScenario && data.effective_scenario && data.effective_scenario !== frozenScenario) {
        const got = SCENARIOS[data.effective_scenario]?.label || data.effective_scenario;
        const want = SCENARIOS[frozenScenario]?.label || frozenScenario;
        showToast(`场景「${want}」未识别，已退化到「${got}」`, "fa-triangle-exclamation");
      }

      const elapsed = Math.round(performance.now() - startedAt);
      const v = frozenVariety;
      const varietyLabel = v.label || data.target_variety || frozenTarget;
      const scriptLabel = SCRIPT_LABEL[data.script || v.script] || data.script || "—";
      const isDegraded = !!data.degraded;

      const recordId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      lastResult = {
        ...data,
        id: recordId,
        original: originalText,
        label: varietyLabel,
        elapsed,
      };

      renderResult(data.rewritten_text, frozenTarget);

      if (isDegraded) {
        setStatus(STATUS_TEXT.degraded(varietyLabel), "error");
        if (degradedTag) degradedTag.hidden = false;
      } else {
        setStatus(STATUS_TEXT.success(varietyLabel, scriptLabel), "success");
      }

      const trialNote = (v.trial)
        ? `⚠ ${varietyLabel}为试用版方言，AI 训练语料较少，输出可能不够稳定，仅供参考。`
        : "";
      warningEl.textContent = [data.warning, trialNote].filter(Boolean).join(" ");

      resultStats.hidden = false;
      resultCharCount.textContent = countChars(data.rewritten_text);
      resultScript.textContent = scriptLabel;
      resultElapsed.textContent = formatElapsed(elapsed);

      copyButton.disabled = false;
      downloadButton.disabled = false;
      favoriteBtn.disabled = false;
      if (explainButton) explainButton.disabled = false;
      if (compareOtherButton) compareOtherButton.disabled = false;
      // TTS gating respects per-variety tts_lang.
      const ttsAvailable = !!v.tts_lang && ("speechSynthesis" in window);
      speakButton.disabled = !ttsAvailable;

      // Push to history (auto-deduped). Favorite defaults to false; user toggles via the button.
      const record = {
        id: recordId,
        ts: Date.now(),
        target_variety: frozenTarget,
        label: varietyLabel,
        script: scriptLabel,
        original: originalText,
        rewritten: data.rewritten_text,
        favorite: false,
        degraded: isDegraded,
        scenario: frozenScenario,
      };
      pushHistory(record);
      syncFavoriteButton(loadHistory().find((r) => r.id === recordId));

      if (compareMode) {
        const orig = resultStage.querySelector(".result-compare-orig");
        if (orig) orig.textContent = originalText;
      }

      showToast(isDegraded ? "已降级为本地启发式" : "重写完成！", isDegraded ? "fa-triangle-exclamation" : "fa-check-circle");
    } catch (err) {
      // Don't display abort errors to user — those are intentional cancellations
      if (err.name !== "AbortError") {
        setStatus(STATUS_TEXT.error(err.message), "error");
      }
    } finally {
      // Only clear busy if we're still the active controller
      if (rewriteAbortController === myController) {
        setBusy(false);
        rewriteAbortController = null;
      }
    }
  }

  // ---------------- multi-variety compare ----------------
  // Tracks variety-key → DOM card so we can toggle/remove.
  const compareCards = new Map();
  const COMPARE_MAX = 3;

  function openCompare() {
    if (!lastResult) return;
    comparePanel.hidden = false;
    renderCompareChips();
    comparePanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  function closeCompare() {
    if (!comparePanel) return;
    comparePanel.hidden = true;
    compareCards.clear();
    if (compareChipsContainer) compareChipsContainer.innerHTML = "";
    if (compareGrid) compareGrid.innerHTML = "";
  }

  function renderCompareChips() {
    if (!compareChipsContainer || !lastResult) return;
    const current = lastResult.target_variety;
    const others = varietyOrder.filter((v) => v !== current);
    compareChipsContainer.innerHTML = others
      .map((v) => {
        const meta = VARIETIES[v];
        return `<button type="button" class="compare-chip" data-variety="${escapeHtml(v)}"
                        title="${escapeHtml(meta.description_short || meta.label)}">
          <i class="fas fa-feather-pointed" aria-hidden="true"></i> ${escapeHtml(meta.label)}
        </button>`;
      })
      .join("");
    $$(".compare-chip", compareChipsContainer).forEach((btn) => {
      btn.addEventListener("click", () => onCompareChipClick(btn));
    });
  }

  async function onCompareChipClick(chip) {
    const v = chip.dataset.variety;
    if (compareCards.has(v)) {
      // Toggle off — remove the card.
      const card = compareCards.get(v);
      card.remove();
      compareCards.delete(v);
      chip.classList.remove("is-active");
      // Re-enable other chips if we drop below max.
      $$(".compare-chip", compareChipsContainer).forEach((c) => {
        if (!compareCards.has(c.dataset.variety)) c.disabled = false;
      });
      return;
    }
    if (compareCards.size >= COMPARE_MAX) {
      showToast(`最多对比 ${COMPARE_MAX} 个方言`, "fa-circle-info");
      return;
    }
    chip.classList.add("is-loading");
    chip.disabled = true;

    const placeholder = renderComparePlaceholder(v);
    compareCards.set(v, placeholder);
    compareGrid.appendChild(placeholder);

    try {
      const res = await fetch("/api/rewrite", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: lastResult.original,
          target_variety: v,
          scenario: SETTINGS.scenario || DEFAULT_SCENARIO,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.status === 429) {
        renderCompareError(placeholder, v, "限流，请稍后再试");
        return;
      }
      if (!res.ok) {
        renderCompareError(placeholder, v, data.error || `HTTP ${res.status}`);
        return;
      }
      renderCompareResult(placeholder, v, data);
      chip.classList.add("is-active");
      // If max reached, disable the still-unselected chips
      if (compareCards.size >= COMPARE_MAX) {
        $$(".compare-chip", compareChipsContainer).forEach((c) => {
          if (!compareCards.has(c.dataset.variety)) c.disabled = true;
        });
      }
    } catch (err) {
      renderCompareError(placeholder, v, err.message || "请求失败");
    } finally {
      chip.classList.remove("is-loading");
      chip.disabled = false;
      // Keep disabled if max reached (re-applied above)
      if (compareCards.size >= COMPARE_MAX && !chip.classList.contains("is-active")) {
        chip.disabled = true;
      }
    }
  }

  function renderComparePlaceholder(varietyKey) {
    const meta = VARIETIES[varietyKey] || {};
    const card = document.createElement("article");
    card.className = "compare-card";
    card.dataset.variety = varietyKey;
    card.innerHTML = `
      <header class="compare-card-head">
        <span class="compare-card-label">
          ${escapeHtml(meta.label || varietyKey)}
          <i class="${SCRIPT_LABEL[meta.script] === "繁體" ? "fas fa-language" : "fas fa-language"}" aria-hidden="true"></i>
          <small>${escapeHtml(SCRIPT_LABEL[meta.script] || meta.script || "")}</small>
        </span>
        <span class="compare-card-actions"></span>
      </header>
      <pre class="compare-card-text"><i class="fas fa-circle-notch fa-spin" aria-hidden="true"></i> 重写中…</pre>
    `;
    return card;
  }

  function renderCompareResult(card, varietyKey, data) {
    const meta = VARIETIES[varietyKey] || {};
    const isDegraded = !!data.degraded;
    if (isDegraded) card.classList.add("is-degraded");
    const text = data.rewritten_text || "";
    const textEl = card.querySelector(".compare-card-text");
    textEl.textContent = text;
    applyKeywordHighlight(textEl, varietyKey);

    const actions = card.querySelector(".compare-card-actions");
    actions.innerHTML = `
      <button class="icon-btn" data-act="copy" title="复制"><i class="far fa-copy" aria-hidden="true"></i></button>
      <button class="icon-btn" data-act="remove" title="移除"><i class="fas fa-xmark" aria-hidden="true"></i></button>
    `;
    actions.querySelector('[data-act="copy"]').addEventListener("click", async () => {
      await navigator.clipboard.writeText(text);
      showToast(`已复制 ${meta.label}`, "fa-clipboard-check");
    });
    actions.querySelector('[data-act="remove"]').addEventListener("click", () => {
      const chip = compareChipsContainer.querySelector(`[data-variety="${CSS.escape(varietyKey)}"]`);
      if (chip) onCompareChipClick(chip);
    });
  }

  function renderCompareError(card, varietyKey, msg) {
    card.classList.add("is-error");
    const textEl = card.querySelector(".compare-card-text");
    textEl.textContent = `× ${msg}`;
  }

  // ---------------- explain rewrite ----------------
  let explainAbort = null;

  function openExplain() {
    if (!lastResult) return;
    explainDrawer.hidden = false;
    explainBody.innerHTML = '<p class="explain-loading"><i class="fas fa-circle-notch fa-spin" aria-hidden="true"></i> AI 正在分析…</p>';
    explainDrawer.scrollIntoView({ behavior: "smooth", block: "nearest" });

    if (explainAbort) explainAbort.abort();
    explainAbort = new AbortController();

    fetch("/api/explain", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        original: lastResult.original,
        rewritten: lastResult.rewritten_text,
        target_variety: lastResult.target_variety,
      }),
      signal: explainAbort.signal,
    })
      .then(async (r) => {
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        renderExplain(d);
      })
      .catch((err) => {
        if (err.name === "AbortError") return;
        explainBody.innerHTML = `<p class="warning">解释失败：${escapeHtml(err.message)}</p>`;
      });
  }

  function closeExplain() {
    if (!explainDrawer) return;
    if (explainAbort) {
      explainAbort.abort();
      explainAbort = null;
    }
    explainDrawer.hidden = true;
  }

  function renderExplain(data) {
    // Expected shape: { summary: "...", points: [{from, to, why}, ...] } or { markdown: "..." }
    if (data.markdown) {
      explainBody.innerHTML = simpleMarkdown(data.markdown);
      return;
    }
    if (Array.isArray(data.points)) {
      const summary = data.summary ? `<p>${escapeHtml(data.summary)}</p>` : "";
      const items = data.points
        .map((p) => {
          const from = p.from ? `<code>${escapeHtml(p.from)}</code>` : "";
          const to = p.to ? `<code>${escapeHtml(p.to)}</code>` : "";
          const arrow = from && to ? " → " : "";
          const why = p.why ? `：${escapeHtml(p.why)}` : "";
          return `<li><strong>${from}${arrow}${to}</strong>${why}</li>`;
        })
        .join("");
      explainBody.innerHTML = `${summary}<ul>${items}</ul>`;
      return;
    }
    explainBody.textContent = JSON.stringify(data, null, 2);
  }

  // Tiny markdown renderer for the explain output (only needs ul/li/bold/code/inline).
  function simpleMarkdown(md) {
    const escaped = escapeHtml(md);
    let html = escaped
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    // Convert "- xxx" lines into <ul><li>...
    const lines = html.split(/\n/);
    const out = [];
    let inList = false;
    lines.forEach((line) => {
      const m = line.match(/^\s*[-*]\s+(.*)$/);
      if (m) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push(`<li>${m[1]}</li>`);
      } else {
        if (inList) { out.push("</ul>"); inList = false; }
        if (line.trim()) out.push(`<p>${line}</p>`);
      }
    });
    if (inList) out.push("</ul>");
    return out.join("");
  }

  // ---------------- batch workbench (Cycle 8) ----------------
  // Self-contained module: input → SSE → table → export.
  // Uses /api/rewrite-batch (server-side iterates) + optional /api/rate per cell.
  const batchInput = $("#batch-input");
  const batchLineCount = $("#batch-line-count");
  const batchPasteCsv = $("#batch-paste-csv");
  const batchClear = $("#batch-clear");
  const batchVarietyChips = $("#batch-variety-chips");
  const batchScenarioChips = $("#batch-scenario-chips");
  const batchScoreToggle = $("#batch-score-toggle");
  const batchRun = $("#batch-run");
  const batchAbort = $("#batch-abort");
  const batchProgress = $("#batch-progress");
  const batchProgressFill = $("#batch-progress-fill");
  const batchProgressText = $("#batch-progress-text");
  const batchTableWrap = $("#batch-table-wrap");
  const batchTableHead = $("#batch-table-head");
  const batchTableBody = $("#batch-table-body");
  const batchToolbarInfo = $("#batch-toolbar-info");
  const batchEmpty = $("#batch-empty");
  const batchExportCsv = $("#batch-export-csv");
  const batchExportJson = $("#batch-export-json");
  const batchExportMd = $("#batch-export-md");

  // Cap selection at 3 to keep cost predictable
  const BATCH_VARIETY_MAX = 3;
  const BATCH_ITEM_MAX = 100;

  let batchVarietySelected = []; // ordered list of variety values
  let batchScenarioSelected = null; // single scenario value
  let batchAbortController = null;
  // batchData[rowIdx] = {original, outputs: {variety: {ok, text, degraded, score, reason, error}}}
  let batchData = [];

  function renderBatchInputs() {
    if (!batchVarietyChips || !varietyOrder.length) return;
    // Default: scenario follows main settings
    if (!batchScenarioSelected) batchScenarioSelected = SETTINGS.scenario || DEFAULT_SCENARIO;
    // Default: pick standard_putonghua + beijing if nothing chosen yet
    if (batchVarietySelected.length === 0) {
      batchVarietySelected = ["standard_putonghua", "beijing_mandarin"].filter((v) => varietyOrder.includes(v));
    }
    batchVarietyChips.innerHTML = varietyOrder.map((v) => {
      const meta = VARIETIES[v];
      const active = batchVarietySelected.includes(v);
      return `<button type="button" class="batch-chip${active ? " is-active" : ""}"
              data-variety="${escapeHtml(v)}" title="${escapeHtml(meta.label)}">${escapeHtml(meta.label)}</button>`;
    }).join("");
    $$(".batch-chip", batchVarietyChips).forEach((c) => {
      c.addEventListener("click", () => toggleBatchVariety(c.dataset.variety));
    });
    // Scenario chips
    batchScenarioChips.innerHTML = scenarioOrder.map((s) => {
      const meta = SCENARIOS[s];
      const active = s === batchScenarioSelected;
      return `<button type="button" class="batch-chip is-scenario${active ? " is-active" : ""}"
              data-scenario="${escapeHtml(s)}" title="${escapeHtml(meta.description)}">${escapeHtml(meta.label)}</button>`;
    }).join("");
    $$(".batch-chip[data-scenario]", batchScenarioChips).forEach((c) => {
      c.addEventListener("click", () => {
        batchScenarioSelected = c.dataset.scenario;
        $$(".batch-chip[data-scenario]", batchScenarioChips).forEach((b) =>
          b.classList.toggle("is-active", b.dataset.scenario === batchScenarioSelected)
        );
      });
    });
    updateBatchLineCount();
  }

  function toggleBatchVariety(v) {
    const idx = batchVarietySelected.indexOf(v);
    if (idx >= 0) {
      batchVarietySelected.splice(idx, 1);
    } else {
      if (batchVarietySelected.length >= BATCH_VARIETY_MAX) {
        showToast(`最多选 ${BATCH_VARIETY_MAX} 个方言`, "fa-circle-info");
        return;
      }
      batchVarietySelected.push(v);
    }
    $$(".batch-chip", batchVarietyChips).forEach((c) =>
      c.classList.toggle("is-active", batchVarietySelected.includes(c.dataset.variety))
    );
  }

  function getBatchItems() {
    const raw = (batchInput.value || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    return raw;
  }

  function updateBatchLineCount() {
    if (!batchLineCount) return;
    const items = getBatchItems();
    const n = items.length;
    batchLineCount.textContent = `${n} 行`;
    if (n > BATCH_ITEM_MAX) {
      batchLineCount.style.color = "var(--warning)";
    } else if (n > 60) {
      batchLineCount.style.color = "var(--gold)";
    } else {
      batchLineCount.style.color = "";
    }
  }

  // CSV parser (RFC 4180 minimal: handles quoted fields with commas/newlines).
  function parseCsv(text) {
    const rows = [];
    let row = [];
    let field = "";
    let i = 0; let inQuotes = false;
    while (i < text.length) {
      const c = text[i];
      if (inQuotes) {
        if (c === '"' && text[i + 1] === '"') { field += '"'; i += 2; continue; }
        if (c === '"') { inQuotes = false; i++; continue; }
        field += c; i++;
        continue;
      }
      if (c === '"') { inQuotes = true; i++; continue; }
      if (c === ",") { row.push(field); field = ""; i++; continue; }
      if (c === "\n" || c === "\r") {
        row.push(field); field = "";
        if (row.length > 0 && !(row.length === 1 && row[0] === "")) rows.push(row);
        row = [];
        if (c === "\r" && text[i + 1] === "\n") i += 2; else i++;
        continue;
      }
      field += c; i++;
    }
    if (field !== "" || row.length > 0) { row.push(field); rows.push(row); }
    return rows;
  }

  async function smartPaste() {
    let text = "";
    try { text = await navigator.clipboard.readText(); }
    catch (_) { showToast("浏览器拒绝访问剪贴板", "fa-triangle-exclamation"); return; }
    if (!text.trim()) { showToast("剪贴板为空", "fa-circle-info"); return; }
    // Heuristic: if any line has commas or quotes likely CSV; pick first column
    if (/[,"]/.test(text)) {
      const rows = parseCsv(text);
      if (rows.length > 1) {
        const firstCol = rows.map((r) => (r[0] || "").trim()).filter(Boolean);
        batchInput.value = firstCol.join("\n");
        updateBatchLineCount();
        showToast(`识别 CSV，已取首列 ${firstCol.length} 行`, "fa-paste");
        return;
      }
    }
    // Default: just paste (one-line-per-item)
    batchInput.value = text;
    updateBatchLineCount();
    showToast("已粘贴", "fa-paste");
  }

  function clearBatchTable() {
    batchData = [];
    batchTableBody.innerHTML = "";
    batchTableWrap.hidden = true;
    batchEmpty.hidden = false;
    batchProgress.hidden = true;
    batchProgressFill.style.width = "0%";
    batchProgressText.textContent = "";
    batchToolbarInfo.textContent = "—";
  }

  function buildTableHead() {
    let head = "<th>#</th><th>原文</th>";
    batchVarietySelected.forEach((v) => {
      const meta = VARIETIES[v] || {};
      head += `<th>${escapeHtml(meta.label || v)}</th>`;
    });
    if (batchScoreToggle.checked) {
      batchVarietySelected.forEach((v) => {
        const meta = VARIETIES[v] || {};
        head += `<th class="sortable" data-sort-variety="${escapeHtml(v)}" title="${escapeHtml(meta.label)} 质量评分（点击排序）">
          <i class="fas fa-star" aria-hidden="true"></i> ${escapeHtml(meta.label.slice(0, 4))}
          <span class="sort-icon"><i class="fas fa-sort"></i></span></th>`;
      });
    }
    batchTableHead.innerHTML = head;
    // wire sort handlers
    $$("th.sortable", batchTableHead).forEach((th) => {
      th.addEventListener("click", () => sortTableByScore(th.dataset.sortVariety, th));
    });
  }

  let _batchSort = { variety: null, dir: 0 };  // dir: 0=none, 1=asc, -1=desc
  function sortTableByScore(variety, thEl) {
    if (_batchSort.variety === variety) {
      _batchSort.dir = _batchSort.dir === -1 ? 1 : -1;  // toggle
    } else {
      _batchSort = { variety, dir: -1 };  // first click = desc (highest first)
    }
    // visual indicator
    $$("th.sortable", batchTableHead).forEach((th) => {
      th.classList.remove("sort-asc", "sort-desc");
      const icon = th.querySelector(".sort-icon i");
      if (icon) icon.className = "fas fa-sort";
    });
    thEl.classList.add(_batchSort.dir === 1 ? "sort-asc" : "sort-desc");
    const icon = thEl.querySelector(".sort-icon i");
    if (icon) icon.className = _batchSort.dir === 1 ? "fas fa-arrow-up-1-9" : "fas fa-arrow-down-9-1";
    // Reorder rows in DOM (keep batchData unchanged for export)
    const rows = $$("tr", batchTableBody);
    const scoreOf = (tr) => {
      const idx = +tr.dataset.idx;
      const out = batchData[idx]?.outputs?.[variety];
      return out?.score ?? -1;
    };
    rows.sort((a, b) => (scoreOf(a) - scoreOf(b)) * _batchSort.dir);
    rows.forEach((tr) => batchTableBody.appendChild(tr));
  }

  function buildSkeletonRows(items) {
    batchTableBody.innerHTML = "";
    items.forEach((item, idx) => {
      const tr = document.createElement("tr");
      tr.dataset.idx = idx;
      let html = `<td class="row-num">${idx + 1}</td><td class="row-original">${escapeHtml(item)}</td>`;
      batchVarietySelected.forEach((v) => {
        html += `<td class="row-output" data-variety="${escapeHtml(v)}"><span class="skeleton-row"></span></td>`;
      });
      if (batchScoreToggle.checked) {
        batchVarietySelected.forEach((v) => {
          html += `<td class="row-score" data-score-variety="${escapeHtml(v)}"><span class="skeleton-row" style="width:60%"></span></td>`;
        });
      }
      tr.innerHTML = html;
      batchTableBody.appendChild(tr);
    });
  }

  function setCellResult(idx, variety, payload) {
    const tr = batchTableBody.querySelector(`tr[data-idx="${idx}"]`);
    if (!tr) return;
    const td = tr.querySelector(`.row-output[data-variety="${CSS.escape(variety)}"]`);
    if (!td) return;
    if (payload.ok) {
      td.classList.toggle("is-degraded", !!payload.degraded);
      td.textContent = payload.rewritten_text;
      // mark winner if previously chosen
      const isWinner = batchData[idx]?.winner === variety;
      td.classList.toggle("is-winner", isWinner);
      if (isWinner) {
        const star = document.createElement("span");
        star.className = "row-winner-mark";
        star.innerHTML = '<i class="fas fa-crown" aria-hidden="true"></i>';
        td.appendChild(star);
      }
      const actions = document.createElement("div");
      actions.className = "row-output-actions";
      actions.innerHTML = `
        <button data-act="winner" class="${isWinner ? "is-winner-btn" : ""}" title="标记为本行赢家"><i class="${isWinner ? "fas" : "far"} fa-crown" aria-hidden="true"></i></button>
        <button data-act="copy" title="复制"><i class="far fa-copy" aria-hidden="true"></i></button>
        <select class="row-scenario-select" data-act="scenario-pick" title="换场景重新生成">
          ${scenarioOrder.map((s) => `<option value="${escapeHtml(s)}"${s === batchScenarioSelected ? " selected" : ""}>${escapeHtml(SCENARIOS[s].label)}</option>`).join("")}
        </select>
        <button data-act="regen" title="重新生成"><i class="fas fa-rotate" aria-hidden="true"></i></button>`;
      td.appendChild(actions);
      actions.querySelector('[data-act="copy"]').addEventListener("click", async (e) => {
        e.stopPropagation();
        await navigator.clipboard.writeText(payload.rewritten_text);
        showToast("已复制", "fa-clipboard-check");
      });
      actions.querySelector('[data-act="regen"]').addEventListener("click", (e) => {
        e.stopPropagation();
        const select = actions.querySelector('[data-act="scenario-pick"]');
        const scenario = select?.value || batchScenarioSelected;
        regenerateCell(idx, variety, scenario);
      });
      actions.querySelector('[data-act="scenario-pick"]').addEventListener("click", (e) => {
        e.stopPropagation();
      });
      actions.querySelector('[data-act="winner"]').addEventListener("click", (e) => {
        e.stopPropagation();
        toggleWinner(idx, variety);
      });
    } else {
      td.classList.add("is-error");
      td.textContent = `× ${payload.error || "失败"}`;
    }
  }

  function toggleWinner(idx, variety) {
    if (!batchData[idx]) return;
    const current = batchData[idx].winner;
    batchData[idx].winner = current === variety ? null : variety;
    saveBatchSnapshot();
    // Re-render the row's outputs to update visual marks
    const tr = batchTableBody.querySelector(`tr[data-idx="${idx}"]`);
    if (!tr) return;
    batchVarietySelected.forEach((v) => {
      const out = batchData[idx].outputs[v];
      if (out?.ok) setCellResult(idx, v, { ok: true, ...out });
    });
  }

  function setCellScore(idx, variety, score, reason) {
    const tr = batchTableBody.querySelector(`tr[data-idx="${idx}"]`);
    if (!tr) return;
    const td = tr.querySelector(`.row-score[data-score-variety="${CSS.escape(variety)}"]`);
    if (!td) return;
    td.title = reason || "";
    td.innerHTML = `<span class="score-num">${score.toFixed(1)}</span><span class="score-out">/5</span>`;
  }

  async function regenerateCell(idx, variety, scenario) {
    const tr = batchTableBody.querySelector(`tr[data-idx="${idx}"]`);
    if (!tr) return;
    const td = tr.querySelector(`.row-output[data-variety="${CSS.escape(variety)}"]`);
    td.classList.remove("is-error");
    td.innerHTML = '<span class="skeleton-row"></span>';
    const sc = scenario || batchScenarioSelected;
    try {
      const res = await fetch("/api/rewrite", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: batchData[idx].original,
          target_variety: variety,
          scenario: sc,
          glossary: getGlossaryLines(),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setCellResult(idx, variety, { ok: false, error: data.error || `HTTP ${res.status}` });
        return;
      }
      const cell = {
        ok: true,
        rewritten_text: data.rewritten_text,
        degraded: !!data.degraded,
        scenario_used: sc,
      };
      batchData[idx].outputs[variety] = cell;
      setCellResult(idx, variety, cell);
      if (batchScoreToggle.checked) maybeScore(idx, variety, data.rewritten_text);
      if (sc !== batchScenarioSelected) {
        showToast(`已用「${SCENARIOS[sc]?.label}」重新生成`, "fa-rotate");
      }
    } catch (err) {
      setCellResult(idx, variety, { ok: false, error: err.message });
    }
  }

  async function maybeScore(idx, variety, rewritten) {
    try {
      const res = await fetch("/api/rate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rewritten,
          target_variety: variety,
          scenario: batchScenarioSelected,
          original: batchData[idx]?.original || "",
        }),
      });
      if (!res.ok) return;
      const data = await res.json();
      batchData[idx].outputs[variety].score = data.score;
      batchData[idx].outputs[variety].reason = data.reason;
      setCellScore(idx, variety, data.score, data.reason);
      // If server says reflection threshold tripped, hint user
      if (data.needs_reflection) {
        // Avoid toast spam: throttle to once per 60s
        if (!maybeScore._lastHint || Date.now() - maybeScore._lastHint > 60000) {
          maybeScore._lastHint = Date.now();
          showToast(`AI 自反馈触发：${VARIETIES[variety]?.label} 平均分偏低，去「设置」可触发 prompt 重写`, "fa-brain");
        }
      }
    } catch (_) { /* ignore — scoring is best-effort */ }
  }

  // Cycle 14: single source of glossary lines. Cached for the duration of one render
  // pass — invalidated whenever the textarea fires `input`. Three callers (workbench
  // rewrite, batch run, batch regen) used to each parse from localStorage independently.
  let _glossaryCache = null;
  function getGlossaryLines() {
    if (_glossaryCache !== null) return _glossaryCache;
    try {
      const raw = (localStorage.getItem("ncga.glossary.v1") || "").trim();
      if (!raw) { _glossaryCache = null; return null; }
      _glossaryCache = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean).slice(0, 30);
      return _glossaryCache;
    } catch (_) { _glossaryCache = null; return null; }
  }
  function invalidateGlossaryCache() { _glossaryCache = null; }

  async function runBatch() {
    const items = getBatchItems();
    if (items.length === 0) { showToast("还没粘贴原文", "fa-circle-info"); batchInput.focus(); return; }
    if (items.length > BATCH_ITEM_MAX) { showToast(`最多 ${BATCH_ITEM_MAX} 行`, "fa-triangle-exclamation"); return; }
    if (batchVarietySelected.length === 0) { showToast("至少选 1 个方言", "fa-circle-info"); return; }

    // Cancel any in-flight batch
    if (batchAbortController) batchAbortController.abort();
    batchAbortController = new AbortController();

    // Reset UI
    batchData = items.map((item) => ({ original: item, outputs: {} }));
    batchEmpty.hidden = true;
    batchTableWrap.hidden = false;
    batchProgress.hidden = false;
    batchProgressFill.style.width = "0%";
    batchProgressText.textContent = "正在排队…";
    buildTableHead();
    buildSkeletonRows(items);
    batchToolbarInfo.textContent = `${items.length} 行 × ${batchVarietySelected.length} 方言 = ${items.length * batchVarietySelected.length} 项`;
    batchAbort.hidden = false;
    batchRun.disabled = true;

    let total = items.length * batchVarietySelected.length;
    let done = 0;
    let okCount = 0;
    let failCount = 0;

    try {
      const res = await fetch("/api/rewrite-batch", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify({
          items,
          target_varieties: batchVarietySelected,
          scenario: batchScenarioSelected,
          glossary: getGlossaryLines(),
          max_parallel: 3,
        }),
        signal: batchAbortController.signal,
      });
      if (res.status === 429) {
        showToast("批量限流，请稍后再试", "fa-gauge-high");
        batchProgressText.textContent = "限流，请稍后";
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done: streamDone } = await reader.read();
        if (streamDone) break;
        buf += decoder.decode(value, { stream: true });
        // Parse SSE events: split on \n\n
        let parts = buf.split("\n\n");
        buf = parts.pop() || "";
        for (const ev of parts) {
          const lines = ev.split("\n");
          let evType = "message";
          let evData = "";
          for (const line of lines) {
            if (line.startsWith("event: ")) evType = line.slice(7).trim();
            else if (line.startsWith("data: ")) evData += (evData ? "\n" : "") + line.slice(6);
          }
          let payload = null;
          try { payload = JSON.parse(evData); } catch (_) { continue; }
          if (evType === "result") {
            done++;
            if (payload.ok) {
              okCount++;
              const cell = {
                ok: true,
                rewritten_text: payload.rewritten_text,
                degraded: !!payload.degraded,
              };
              batchData[payload.idx].outputs[payload.variety] = cell;
              setCellResult(payload.idx, payload.variety, cell);
              if (batchScoreToggle.checked) {
                // Fire-and-forget score request; doesn't block table fill
                maybeScore(payload.idx, payload.variety, payload.rewritten_text);
              }
            } else {
              failCount++;
              batchData[payload.idx].outputs[payload.variety] = { ok: false, error: payload.error };
              setCellResult(payload.idx, payload.variety, payload);
            }
            const pct = Math.round((done / total) * 100);
            batchProgressFill.style.width = `${pct}%`;
            batchProgressText.textContent = `${done} / ${total} · ${okCount} ✓ · ${failCount} ✗`;
          } else if (evType === "done") {
            batchProgressFill.style.width = "100%";
            const summary = payload.summary || { ok: okCount, failed: failCount, total };
            batchProgressText.textContent = `完成：${summary.ok} ✓ · ${summary.failed} ✗`;
            showToast(`批量完成 ${summary.ok}/${summary.total}`, "fa-circle-check");
          } else if (evType === "error") {
            showToast(`错误：${payload.error || "未知"}`, "fa-triangle-exclamation");
          }
        }
      }
    } catch (err) {
      if (err.name === "AbortError") {
        batchProgressText.textContent = "已中止";
      } else {
        showToast(`批量出错：${err.message}`, "fa-triangle-exclamation");
        batchProgressText.textContent = `出错：${err.message}`;
      }
    } finally {
      batchAbort.hidden = true;
      batchRun.disabled = false;
      batchAbortController = null;
      // Persist last batch result to localStorage for reuse
      saveBatchSnapshot();
    }
  }

  function abortBatch() {
    if (batchAbortController) batchAbortController.abort();
  }

  function saveBatchSnapshot() {
    try {
      localStorage.setItem("ncga.batch.v1", JSON.stringify({
        ts: Date.now(),
        scenario: batchScenarioSelected,
        varieties: batchVarietySelected,
        data: batchData,
      }));
    } catch (_) { /* ignore quota errors */ }
  }

  // ---- exports ----
  function csvEscape(s) {
    s = String(s ?? "");
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }

  function isWinnersOnly() {
    return $("#batch-winners-only")?.checked && batchData.some((r) => r?.winner);
  }

  function exportCsv() {
    if (!batchData.length) { showToast("还没有结果", "fa-circle-info"); return; }
    const winnersOnly = isWinnersOnly();
    const header = winnersOnly
      ? ["#", "原文", "赢家方言", "改写", "评分"]
      : ["#", "原文", ...batchVarietySelected.map((v) => VARIETIES[v]?.label || v)];
    if (!winnersOnly && batchScoreToggle.checked) {
      header.push(...batchVarietySelected.map((v) => `${VARIETIES[v]?.label || v} 评分`));
    }
    const rows = batchData
      .filter((row) => !winnersOnly || row.winner)
      .map((row, i) => {
        if (winnersOnly) {
          const v = row.winner;
          const out = row.outputs[v] || {};
          return [i + 1, row.original, VARIETIES[v]?.label || v,
                  out.ok ? out.rewritten_text : "", out.score ?? ""].map(csvEscape).join(",");
        }
        const cells = [i + 1, row.original];
        for (const v of batchVarietySelected) {
          const out = row.outputs[v];
          cells.push(out?.ok ? out.rewritten_text : (out?.error ? `[ERROR] ${out.error}` : ""));
        }
        if (batchScoreToggle.checked) {
          for (const v of batchVarietySelected) {
            const out = row.outputs[v];
            cells.push(out?.score != null ? out.score : "");
          }
        }
        return cells.map(csvEscape).join(",");
      });
    const text = "﻿" + [header.map(csvEscape).join(","), ...rows].join("\r\n");  // BOM for Excel
    downloadFile(text, `批量重写_${winnersOnly ? "赢家_" : ""}${dateStamp()}.csv`, "text/csv;charset=utf-8");
  }

  function exportJson() {
    if (!batchData.length) { showToast("还没有结果", "fa-circle-info"); return; }
    const out = {
      generated_at: new Date().toISOString(),
      scenario: batchScenarioSelected,
      varieties: batchVarietySelected.map((v) => ({ value: v, label: VARIETIES[v]?.label })),
      rows: batchData,
    };
    downloadFile(JSON.stringify(out, null, 2), `批量重写_${dateStamp()}.json`, "application/json");
  }

  function exportMd() {
    if (!batchData.length) { showToast("还没有结果", "fa-circle-info"); return; }
    const lines = [`# 批量重写结果 (${batchData.length} 条)`, "", `场景：${SCENARIOS[batchScenarioSelected]?.label || batchScenarioSelected}`, ""];
    batchData.forEach((row, i) => {
      lines.push(`## ${i + 1}. ${row.original}`);
      for (const v of batchVarietySelected) {
        const out = row.outputs[v];
        const label = VARIETIES[v]?.label || v;
        if (out?.ok) {
          const score = out.score != null ? ` (评分 ${out.score}/5)` : "";
          lines.push(`- **${label}**${score}: ${out.rewritten_text}`);
        } else if (out?.error) {
          lines.push(`- **${label}**: \`× ${out.error}\``);
        }
      }
      lines.push("");
    });
    downloadFile(lines.join("\n"), `批量重写_${dateStamp()}.md`, "text/markdown;charset=utf-8");
  }

  function downloadFile(text, filename, mime) {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
    showToast(`已导出 ${filename}`, "fa-file-arrow-down");
  }

  function dateStamp() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}`;
  }

  // ---------------- glossary + AI self-feedback panel (Cycle 9) ----------------
  // ---------------- Voice input (Cycle 15) ----------------
  function bindVoiceInput() {
    const btn = $("#mic-button");
    if (!btn) return;
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      // Browser doesn't support SpeechRecognition (Firefox at the time of writing,
      // Safari < 14.1) — keep the button hidden. The aria-label is still there for AT.
      return;
    }
    btn.hidden = false;

    let recognition = null;
    let recording = false;

    const startRec = () => {
      try {
        recognition = new SR();
      } catch (e) {
        showToast("浏览器不支持语音识别", "fa-triangle-exclamation");
        return;
      }
      recognition.lang = "zh-CN";
      recognition.continuous = false;
      recognition.interimResults = true;
      recognition.maxAlternatives = 1;

      // Snapshot the textarea content at start so interim results don't keep replacing
      // already-finalized text.
      const existing = textInput.value;
      const insertionPoint = existing ? existing + (existing.endsWith("\n") ? "" : " ") : "";
      let lastFinalLen = 0;

      recognition.onstart = () => {
        recording = true;
        btn.classList.add("is-recording");
        btn.title = "再次点击停止";
        showToast("开始听…说中文", "fa-microphone");
      };
      recognition.onresult = (event) => {
        let interim = "";
        let final = "";
        for (let i = event.resultIndex; i < event.results.length; i++) {
          const transcript = event.results[i][0].transcript;
          if (event.results[i].isFinal) final += transcript;
          else interim += transcript;
        }
        if (final) {
          // Append final text to textarea; keep interim separate so it doesn't pile up
          textInput.value = insertionPoint + textInput.value.slice(insertionPoint.length).slice(0, lastFinalLen) + final;
          lastFinalLen += final.length;
        } else {
          // Show interim by appending to existing finalized portion
          const finalSoFar = (insertionPoint || "") + (textInput.value.slice(insertionPoint.length, insertionPoint.length + lastFinalLen) || "");
          textInput.value = finalSoFar + interim;
        }
        textInput.dispatchEvent(new Event("input"));
      };
      recognition.onerror = (event) => {
        const msg = {
          "not-allowed": "麦克风权限被拒绝，请在浏览器设置中允许",
          "service-not-allowed": "浏览器或系统禁用了语音服务",
          "no-speech": "没听到声音，请再试一次",
          "audio-capture": "找不到麦克风设备",
          "aborted": null,  // user pressed stop — silent
          "network": "语音识别服务网络异常",
        }[event.error] || `语音识别错误：${event.error}`;
        if (msg) showToast(msg, "fa-triangle-exclamation");
      };
      recognition.onend = () => {
        recording = false;
        btn.classList.remove("is-recording");
        btn.title = "点击开始语音输入（中文）";
        recognition = null;
      };
      try {
        recognition.start();
      } catch (e) {
        showToast(`语音输入启动失败：${e.message}`, "fa-triangle-exclamation");
        btn.classList.remove("is-recording");
        recording = false;
      }
    };

    const stopRec = () => {
      if (recognition && recording) {
        try { recognition.stop(); } catch (_) { /* ignore */ }
      }
    };

    btn.addEventListener("click", () => {
      if (recording) stopRec();
      else startRec();
    });

    // Esc while recording stops it
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && recording) {
        e.preventDefault();
        stopRec();
      }
    });
  }

  function bindGlossary() {
    const ta = $("#glossary-input");
    const hint = $("#glossary-hint");
    if (!ta) return;
    try { ta.value = localStorage.getItem("ncga.glossary.v1") || ""; } catch (_) {}
    const update = () => {
      try { localStorage.setItem("ncga.glossary.v1", ta.value); } catch (_) {}
      invalidateGlossaryCache();  // Cycle 14: keep cache fresh after edit
      const lines = ta.value.split(/\r?\n/).filter((l) => l.trim()).length;
      if (hint) hint.textContent = lines === 0 ? "未启用" : `已启用 · ${lines} 条术语将注入 prompt`;
    };
    ta.addEventListener("input", update);
    update();
  }

  async function loadQualityStats() {
    const panel = $("#quality-stats");
    if (!panel) return;
    try {
      const res = await fetch("/api/quality-stats");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      const buckets = data.buckets || [];
      if (!buckets.length) {
        panel.innerHTML = '<p class="settings-hint">还没有评分数据。在批量工作台开启「质量评分」并跑一次，数据会出现在这里。</p>';
        return;
      }
      // Sort by mean ascending — worst first (most likely to need attention)
      buckets.sort((a, b) => (a.stats?.mean ?? 5) - (b.stats?.mean ?? 5));
      panel.innerHTML = buckets.map(renderQualityBucket).join("");
      // Cycle 20: bar width is dynamic per bucket; setting via DOM API after
      // insertion keeps the HTML CSP-clean (no inline style=).
      $$(".quality-bucket-distribution .fill[data-width]", panel).forEach((el) => {
        const pct = Math.max(0, Math.min(100, parseFloat(el.dataset.width || "0")));
        el.style.width = pct + "%";
      });
      $$(".quality-bucket [data-act='refine']", panel).forEach((btn) => {
        btn.addEventListener("click", () => triggerMetaRefine(btn.dataset.variety, btn.dataset.scenario, btn));
      });
      $$(".quality-bucket [data-act='clear-override']", panel).forEach((btn) => {
        btn.addEventListener("click", () => clearOverride(btn.dataset.variety, btn.dataset.scenario));
      });
      $$(".quality-bucket [data-act='review-draft']", panel).forEach((btn) => {
        btn.addEventListener("click", () => openOverrideReview(btn.dataset.variety, btn.dataset.scenario));
      });
      $$(".quality-bucket [data-act='reject-draft']", panel).forEach((btn) => {
        btn.addEventListener("click", () => rejectDraft(btn.dataset.variety, btn.dataset.scenario));
      });
    } catch (err) {
      panel.innerHTML = `<p class="warning">加载失败：${escapeHtml(err.message)}</p>`;
    }
  }

  function renderQualityBucket(b) {
    const v = VARIETIES[b.variety] || {};
    const s = SCENARIOS[b.scenario] || {};
    const stats = b.stats || {};
    const meanPct = Math.max(0, Math.min(100, (stats.mean / 5) * 100));
    const classes = ["quality-bucket"];
    if (b.has_override) classes.push("has-override");
    if (b.has_draft) classes.push("has-draft");
    if (b.needs_reflection) classes.push("needs-reflection");

    // A/B delta block (Cycle 10): only shown when override active and post samples exist
    let abBlock = "";
    if (b.ab && b.ab.post_count > 0) {
      const d = b.ab.delta;
      const arrow = d > 0.05 ? "↑" : d < -0.05 ? "↓" : "→";
      const cls = d > 0.05 ? "improved" : d < -0.05 ? "degraded" : "";
      const sign = d > 0 ? "+" : "";
      abBlock = `<div class="quality-bucket-ab ${cls}" title="启用 override 前 ${b.ab.baseline_count} 样本均值 vs 启用后 ${b.ab.post_count} 样本均值">
          A/B: ${b.ab.baseline_mean} <span class="delta-arrow">${arrow}</span> ${b.ab.post_mean} (${sign}${d})
        </div>`;
    }

    // Draft block: shows when has_draft. Click "审核" opens review modal.
    let draftBlock = "";
    if (b.has_draft) {
      draftBlock = `<div class="quality-bucket-draft">
        <span class="draft-label"><i class="fas fa-file-pen" aria-hidden="true"></i> 待审核草稿</span>
        <span>${escapeHtml(b.draft_reason || "(无诊断)")}</span>
        <div class="quality-bucket-actions">
          <button class="primary-btn" data-act="review-draft" data-variety="${escapeHtml(b.variety)}" data-scenario="${escapeHtml(b.scenario)}">
            <i class="fas fa-eye" aria-hidden="true"></i> 审核草稿
          </button>
          <button class="ghost-btn danger" data-act="reject-draft" data-variety="${escapeHtml(b.variety)}" data-scenario="${escapeHtml(b.scenario)}">
            <i class="fas fa-xmark" aria-hidden="true"></i> 直接拒绝
          </button>
        </div>
      </div>`;
    }

    return `
      <div class="${classes.join(" ")}">
        <div class="quality-bucket-head">
          <span>${escapeHtml(v.label || b.variety)}</span>
          <span class="scenario-tag">${escapeHtml(s.label || b.scenario)}</span>
        </div>
        <div class="quality-bucket-stats">
          <span class="stat"><span class="lbl">μ</span><span class="num">${(stats.mean ?? 0).toFixed(2)}</span></span>
          <span class="stat"><span class="lbl">σ</span><span class="num">${(stats.stddev ?? 0).toFixed(2)}</span></span>
          <span class="stat"><span class="lbl">n</span><span class="num">${stats.count}</span></span>
          <span class="stat"><span class="lbl">min/max</span><span class="num">${stats.min ?? "—"}/${stats.max ?? "—"}</span></span>
        </div>
        <div class="quality-bucket-distribution" title="平均分 ${(stats.mean ?? 0).toFixed(2)}/5">
          <div class="fill" data-width="${meanPct}"></div>
          <div class="marker" title="阈值 3.5"></div>
        </div>
        ${abBlock}
        ${draftBlock}
        ${b.has_override ? `<div class="quality-bucket-override"><strong>已激活自定义指引</strong>（基线 μ=${(b.override_baseline_mean ?? 0).toFixed(2)}）：${escapeHtml(b.override_reason || "")}</div>` : ""}
        <div class="quality-bucket-actions">
          <button class="ghost-btn" data-act="refine" data-variety="${escapeHtml(b.variety)}" data-scenario="${escapeHtml(b.scenario)}"
                  ${b.needs_reflection || b.has_draft ? "" : 'title="需要 ≥8 样本且 μ<3.5；或直接强制触发"'}>
            <i class="fas fa-brain" aria-hidden="true"></i> ${b.has_override ? "再次自反馈" : (b.has_draft ? "重新生成草稿" : "触发自反馈")}
          </button>
          ${b.has_override ? `<button class="ghost-btn danger" data-act="clear-override" data-variety="${escapeHtml(b.variety)}" data-scenario="${escapeHtml(b.scenario)}">
            <i class="fas fa-rotate-left" aria-hidden="true"></i> 还原默认
          </button>` : ""}
        </div>
      </div>`;
  }

  async function triggerMetaRefine(variety, scenario, btn) {
    if (!variety || !scenario) return;
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="fas fa-circle-notch fa-spin" aria-hidden="true"></i> AI 反思中…';
    try {
      const res = await fetch("/api/meta-refine", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_variety: variety, scenario }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      // Cycle 10: meta-refine now writes a DRAFT; open the review modal so user
      // can approve/reject before it goes live.
      showToast("AI 写好草稿，等你审核", "fa-file-pen");
      await loadQualityStats();
      openOverrideReview(variety, scenario);
    } catch (err) {
      showToast(`自反馈失败：${err.message}`, "fa-triangle-exclamation");
      btn.innerHTML = orig;
      btn.disabled = false;
    }
  }

  // ---------- Override review modal (Cycle 10) ----------
  const overrideModal = $("#override-modal");
  const overrideModalTitle = $("#override-modal-title");
  const overrideModalSub = $("#override-modal-sub");
  const overrideModalOld = $("#override-modal-old");
  const overrideModalNew = $("#override-modal-new");
  const overrideModalReason = $("#override-modal-reason");
  const overrideModalBaseline = $("#override-modal-baseline");
  const overrideActivateBtn = $("#override-activate-btn");
  const overrideRejectBtn = $("#override-reject-btn");
  const overrideModalCancel = $("#override-modal-cancel");
  const overrideModalClose = $("#override-modal-close");
  let _reviewState = null;  // { variety, scenario }

  async function openOverrideReview(variety, scenario) {
    _reviewState = { variety, scenario };
    // Fetch fresh stats so we always show the current draft
    let bucket = null;
    try {
      const res = await fetch("/api/quality-stats");
      const data = await res.json();
      bucket = (data.buckets || []).find(
        (b) => b.variety === variety && b.scenario === scenario,
      );
    } catch (_) { /* ignore */ }
    if (!bucket || !bucket.has_draft) {
      showToast("此 (方言, 场景) 当前没有草稿可审核", "fa-circle-info");
      return;
    }
    const v = VARIETIES[variety] || {};
    const s = SCENARIOS[scenario] || {};
    overrideModalTitle.textContent = `${v.label || variety} · ${s.label || scenario} · 审核草稿`;
    // Show current addendum (active override or default scenario addendum)
    overrideModalOld.textContent =
      bucket.override_reason
        ? `(已激活的旧指引)\n${bucket.override_reason}`  // active override exists
        : `(场景默认指引)\n${s.description || ""}`;
    overrideModalNew.value = bucket.draft_addendum || "";
    overrideModalReason.textContent = bucket.draft_reason || "—";
    overrideModalBaseline.textContent = (bucket.draft_baseline_mean ?? bucket.stats?.mean ?? 0).toFixed(2);
    overrideModal.hidden = false;
    setTimeout(() => overrideModalNew.focus(), 60);
  }

  function closeOverrideReview() {
    overrideModal.hidden = true;
    _reviewState = null;
  }

  async function activateReviewedOverride() {
    if (!_reviewState) return;
    const { variety, scenario } = _reviewState;
    const edited = overrideModalNew.value.trim();
    if (!edited) {
      showToast("不能为空", "fa-triangle-exclamation");
      return;
    }
    overrideActivateBtn.disabled = true;
    try {
      const res = await fetch("/api/override-activate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_variety: variety,
          scenario,
          addendum: edited,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      showToast("新指引已启用", "fa-check-circle");
      closeOverrideReview();
      await loadQualityStats();
    } catch (err) {
      showToast(`启用失败：${err.message}`, "fa-triangle-exclamation");
    } finally {
      overrideActivateBtn.disabled = false;
    }
  }

  async function rejectDraft(variety, scenario) {
    if (!variety || !scenario) {
      if (!_reviewState) return;
      ({ variety, scenario } = _reviewState);
    }
    if (!confirm("确认丢弃这份草稿？(冷却期仍生效，5 分钟内不会再自动触发)")) return;
    try {
      const res = await fetch("/api/override-reject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_variety: variety, scenario }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      showToast("草稿已拒绝", "fa-rotate-left");
      closeOverrideReview();
      await loadQualityStats();
    } catch (err) { showToast(`失败：${err.message}`, "fa-triangle-exclamation"); }
  }

  if (overrideActivateBtn) overrideActivateBtn.addEventListener("click", activateReviewedOverride);
  if (overrideRejectBtn) overrideRejectBtn.addEventListener("click", () => rejectDraft());
  if (overrideModalCancel) overrideModalCancel.addEventListener("click", closeOverrideReview);
  if (overrideModalClose) overrideModalClose.addEventListener("click", closeOverrideReview);
  if (overrideModal) {
    overrideModal.addEventListener("click", (e) => {
      if (e.target === overrideModal) closeOverrideReview();
    });
  }

  async function clearOverride(variety, scenario) {
    if (!confirm(`确认还原 ${VARIETIES[variety]?.label} / ${SCENARIOS[scenario]?.label} 的默认指引？`)) return;
    try {
      const res = await fetch("/api/quality-stats/clear-override", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_variety: variety, scenario }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      showToast("已还原默认", "fa-rotate-left");
      await loadQualityStats();
    } catch (err) { showToast(`失败：${err.message}`, "fa-triangle-exclamation"); }
  }

  function bindBatch() {
    if (!batchInput) return;
    batchInput.addEventListener("input", updateBatchLineCount);
    batchPasteCsv.addEventListener("click", smartPaste);
    batchClear.addEventListener("click", () => {
      batchInput.value = "";
      updateBatchLineCount();
      clearBatchTable();
    });
    batchRun.addEventListener("click", runBatch);
    batchAbort.addEventListener("click", abortBatch);
    batchExportCsv.addEventListener("click", exportCsv);
    batchExportJson.addEventListener("click", exportJson);
    batchExportMd.addEventListener("click", exportMd);
    batchScoreToggle.addEventListener("change", () => {
      // Re-render head if there are results, otherwise just remember preference
      if (batchData.length) {
        buildTableHead();
        // Add or remove the score columns from each row
        $$("tr", batchTableBody).forEach((tr) => {
          const idx = +tr.dataset.idx;
          // Strip existing score cells
          tr.querySelectorAll(".row-score").forEach((c) => c.remove());
          if (batchScoreToggle.checked) {
            for (const v of batchVarietySelected) {
              const td = document.createElement("td");
              td.className = "row-score";
              td.dataset.scoreVariety = v;
              const out = batchData[idx]?.outputs?.[v];
              if (out?.ok) {
                if (out.score != null) {
                  td.title = out.reason || "";
                  td.innerHTML = `<span class="score-num">${out.score.toFixed(1)}</span><span class="score-out">/5</span>`;
                } else {
                  td.innerHTML = '<span class="skeleton-row skeleton-row--short"></span>';
                  maybeScore(idx, v, out.rewritten_text);
                }
              }
              tr.appendChild(td);
            }
          }
        });
      }
    });
    // Keyboard inside batch page
    document.addEventListener("keydown", (e) => {
      if (document.body.getAttribute("data-route") !== "batch") return;
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        runBatch();
      } else if ((e.metaKey || e.ctrlKey) && (e.key === "e" || e.key === "E")) {
        e.preventDefault();
        exportCsv();
      } else if (e.key === "Escape" && !batchAbort.hidden) {
        e.preventDefault();
        abortBatch();
      }
    });
  }

  // ---------------- bindings ----------------
  function bindExampleChips() {
    // Cycle 18 v2 — the prior `window.confirm` was rough UX and easy to misread.
    // Replaced with a dedicated 3-button modal: 套用示例 / 保留原文 / 取消.
    // Empty textarea or identical text → fill silently with no prompt.
    const modal = $("#example-overwrite-modal");
    const elCurrent = $("#example-overwrite-current");
    const elIncoming = $("#example-overwrite-incoming");
    const btnLoad = $("#example-overwrite-load");
    const btnKeep = $("#example-overwrite-keep");
    const btnCancel = $("#example-overwrite-cancel");
    let pendingIncoming = "";

    function openOverwriteModal(currentText, incomingText) {
      pendingIncoming = incomingText;
      elCurrent.textContent = currentText;
      elIncoming.textContent = incomingText;
      modal.hidden = false;
      requestAnimationFrame(() => btnLoad.focus());
    }
    function closeOverwriteModal() {
      modal.hidden = true;
      pendingIncoming = "";
      textInput.focus();
    }
    btnLoad.addEventListener("click", () => {
      if (pendingIncoming) {
        textInput.value = pendingIncoming;
        updateCharCount();
      }
      closeOverwriteModal();
    });
    btnKeep.addEventListener("click", closeOverwriteModal);
    btnCancel.addEventListener("click", closeOverwriteModal);
    modal.addEventListener("click", (e) => { if (e.target === modal) closeOverwriteModal(); });
    document.addEventListener("keydown", (e) => {
      if (!modal.hidden && e.key === "Escape") { e.preventDefault(); closeOverwriteModal(); }
    });

    $$(".chip[data-example]").forEach((chip) => {
      chip.addEventListener("click", () => {
        const incoming = chip.dataset.example;
        const current = textInput.value;
        if (!current.trim() || current.trim() === incoming.trim()) {
          textInput.value = incoming;
          updateCharCount();
          textInput.focus();
          return;
        }
        openOverwriteModal(current, incoming);
      });
    });
  }

  // ---------------- Cycle 18 — 情境向导 (Function 1) ----------------
  function bindContextWizard() {
    const openBtn = $("#context-wizard-open");
    const modal = $("#context-wizard-modal");
    if (!openBtn || !modal) return;
    const closeBtn = $("#context-wizard-close");
    const cancelBtn = $("#context-wizard-cancel");
    const submitBtn = $("#context-wizard-submit");
    const doneBtn = $("#context-wizard-done");
    const stepInput = $("#context-wizard-step-input");
    const stepResult = $("#context-wizard-step-result");
    const recipientEl = $("#context-wizard-recipient");
    const moodEl = $("#context-wizard-mood");
    const outScenario = $("#cw-result-scenario");
    const outRegister = $("#cw-result-register");
    const outTone = $("#cw-result-tone");
    const outChips = $("#cw-result-glossary-chips");
    const applyScenarioBtn = $("#cw-apply-scenario");

    function reset() {
      stepInput.hidden = false;
      stepResult.hidden = true;
      submitBtn.hidden = false;
      doneBtn.hidden = true;
      recipientEl.value = "";
      moodEl.value = "";
      outScenario.textContent = "—";
      outRegister.textContent = "—";
      outTone.textContent = "—";
      outChips.innerHTML = "";
      applyScenarioBtn.dataset.scenario = "";
    }
    function open() {
      reset();
      modal.hidden = false;
      requestAnimationFrame(() => recipientEl.focus());
    }
    function close() {
      modal.hidden = true;
      // Cycle 18 v2 (A15): reset on close. Reopening always starts fresh — the
      // user expects a "new analysis" not "previous analysis still here." Earlier
      // we only reset on open(); that left a brief flash of stale inputs while
      // the modal animated in. Resetting on close eliminates the flash entirely.
      reset();
    }
    openBtn.addEventListener("click", open);
    closeBtn.addEventListener("click", close);
    cancelBtn.addEventListener("click", close);
    doneBtn.addEventListener("click", close);
    modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
    document.addEventListener("keydown", (e) => {
      if (!modal.hidden && e.key === "Escape") { e.preventDefault(); close(); }
    });

    submitBtn.addEventListener("click", async () => {
      const recipient = recipientEl.value.trim();
      const mood = moodEl.value.trim();
      if (!recipient && !mood) { showToast("两个问题至少填一个", "fa-circle-exclamation"); return; }
      submitBtn.disabled = true;
      const oldHtml = submitBtn.innerHTML;
      submitBtn.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i> 分析中…';
      try {
        const res = await fetch("/api/characterize", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ recipient, mood }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.status === 429) { showToast("情境向导用得太快了，等一分钟再试", "fa-hourglass"); return; }
        if (!res.ok) { showToast(data.error || "分析失败", "fa-circle-exclamation"); return; }
        // Render results
        const scenarioMeta = SCENARIOS[data.suggested_scenario];
        outScenario.textContent = scenarioMeta ? scenarioMeta.label : (data.suggested_scenario || "—");
        applyScenarioBtn.dataset.scenario = data.suggested_scenario || "";
        outRegister.textContent = data.register_hint || "—";
        outTone.textContent = data.emotional_tone || "—";
        outChips.innerHTML = "";
        (data.glossary_suggestions || []).slice(0, 5).forEach((entry) => {
          const text = String(entry || "").trim();
          if (!text) return;
          const chip = document.createElement("button");
          chip.type = "button";
          chip.className = "cw-glossary-chip";
          chip.innerHTML = `<i class="fas fa-plus"></i> ${escapeHtml(text)}`;
          chip.addEventListener("click", () => {
            if (chip.classList.contains("is-added")) return;
            const ta = $("#glossary-textarea");
            const cur = (ta?.value || (localStorage.getItem("ncga.glossary.v1") || "")).trim();
            const lines = cur ? cur.split(/\r?\n/) : [];
            if (!lines.includes(text)) lines.push(text);
            const next = lines.slice(0, 30).join("\n");
            if (ta) ta.value = next;
            localStorage.setItem("ncga.glossary.v1", next);
            invalidateGlossaryCache();
            chip.classList.add("is-added");
            chip.innerHTML = `<i class="fas fa-check"></i> ${escapeHtml(text)} 已加入字典`;
          });
          outChips.appendChild(chip);
        });
        stepInput.hidden = true;
        stepResult.hidden = false;
        submitBtn.hidden = true;
        doneBtn.hidden = false;
      } catch (err) {
        showToast("网络异常：" + (err?.message || ""), "fa-circle-exclamation");
      } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = oldHtml;
      }
    });

    applyScenarioBtn.addEventListener("click", () => {
      const v = applyScenarioBtn.dataset.scenario;
      if (!v || !SCENARIOS[v]) { showToast("没有可用的场景建议", "fa-circle-exclamation"); return; }
      SETTINGS.scenario = v;
      saveSettings();
      // Re-render scenario chips so the active one updates
      renderScenarioChips();
      showToast(`场景已切换到「${SCENARIOS[v].label}」`, "fa-comments");
      // F5 fix (Cycle 18 v2): auto-close after a beat so the toast has time to start
      // and the user gets explicit feedback that the scenario was applied. Without
      // this, the wizard window stayed open and the apply step felt like a no-op.
      setTimeout(close, 380);
    });
  }

  // ---------------- Cycle 18 — 今日方言一句 (Function 2) ----------------
  function bindDailyPhrase() {
    const card = $("#daily-phrase-card");
    if (!card) return;
    const dateEl = $("#daily-phrase-date");
    const slidesEl = $("#daily-phrase-slides");
    const imgCap = $("#daily-phrase-image-caption");
    const origEl = $("#daily-phrase-original");
    const meanEl = $("#daily-phrase-meaning");
    const listEl = $("#daily-phrase-translations-list");
    const countEl = $("#daily-phrase-translations-count");
    const favBtn = $("#daily-phrase-fav");
    const favIcon = favBtn?.querySelector("i");
    // F2 (Cycle 18 v2): viewer wiring for the localStorage saved list.
    const savedBtn = $("#daily-phrase-saved");
    const savedCountEl = $("#daily-phrase-saved-count");
    const savedModal = $("#saved-phrases-modal");
    const savedClose = $("#saved-phrases-close");
    const savedListEl = $("#saved-phrases-list");
    const savedEmptyEl = $("#saved-phrases-empty");
    // Cycle 19 v2: DISMISS_KEY removed (no more dismiss-per-day flow).
    const FAV_KEY = "ncga.daily-phrase.saved.v1";
    let slideTimer = null;

    function readSaved() {
      try { return JSON.parse(localStorage.getItem(FAV_KEY) || "[]"); }
      catch { return []; }
    }
    function writeSaved(list) {
      localStorage.setItem(FAV_KEY, JSON.stringify(list.slice(0, 60)));
    }
    function refreshSavedButton() {
      const list = readSaved();
      if (!savedBtn) return;
      if (list.length > 0) {
        savedBtn.hidden = false;
        if (savedCountEl) savedCountEl.textContent = String(list.length);
      } else {
        savedBtn.hidden = true;
      }
    }
    function renderSavedList() {
      const list = readSaved();
      if (!savedListEl || !savedEmptyEl) return;
      if (list.length === 0) {
        savedListEl.innerHTML = "";
        savedEmptyEl.hidden = false;
        return;
      }
      savedEmptyEl.hidden = true;
      savedListEl.innerHTML = list.map((s, i) => `
        <li data-idx="${i}">
          <div class="saved-phrases-row">
            <span class="date">${escapeHtml(s.date || "—")}</span>
            <button type="button" class="delete-btn" data-del="${i}" title="删除">
              <i class="fas fa-trash" aria-hidden="true"></i>
            </button>
          </div>
          <blockquote class="saved-phrases-quote">${escapeHtml(s.original_phrase || "")}</blockquote>
          <p class="saved-phrases-meaning">${escapeHtml(s.meaning || "")}</p>
        </li>
      `).join("");
      // Wire per-item delete
      $$("button.delete-btn", savedListEl).forEach((btn) => {
        btn.addEventListener("click", () => {
          const idx = +btn.dataset.del;
          const cur = readSaved();
          cur.splice(idx, 1);
          writeSaved(cur);
          renderSavedList();
          refreshSavedButton();
          // If the currently-shown phrase was just unsaved, flip the bookmark icon back.
          if (favIcon) favIcon.className = "far fa-bookmark";
        });
      });
    }
    function openSavedModal() {
      renderSavedList();
      savedModal.hidden = false;
    }
    function closeSavedModal() { savedModal.hidden = true; }
    if (savedBtn) savedBtn.addEventListener("click", openSavedModal);
    if (savedClose) savedClose.addEventListener("click", closeSavedModal);
    if (savedModal) savedModal.addEventListener("click", (e) => { if (e.target === savedModal) closeSavedModal(); });
    document.addEventListener("keydown", (e) => {
      if (savedModal && !savedModal.hidden && e.key === "Escape") { e.preventDefault(); closeSavedModal(); }
    });

    function startSlideshow(images) {
      // Cycle 18 v2 (per A2): on first load, run a FAST sweep through all 12
      // landmarks (~220ms each ≈ 2.6s total) — visually says "wisdom is shared
      // across all of these places." THEN settle into a slow 3.2s rotation. The
      // phrase text fades in only after the sweep completes, so the eye is drawn
      // to the geographic montage first and the wisdom-line second.
      slidesEl.innerHTML = "";
      if (!images || !images.length) {
        slidesEl.parentElement.style.display = "none";
        return;
      }
      const textEl = card.querySelector(".daily-phrase-text");
      if (textEl) textEl.classList.add("is-warming");
      images.forEach((img, i) => {
        const el = document.createElement("img");
        el.src = img.url;
        el.alt = img.caption || "";
        el.loading = i === 0 ? "eager" : "lazy";  // first image fast, rest lazy
        if (i === 0) el.classList.add("is-active");
        slidesEl.appendChild(el);
      });
      imgCap.textContent = images[0].caption || "";
      const all = slidesEl.querySelectorAll("img");
      let idx = 0;
      const SWEEP_MS = 220;   // fast intro
      const REST_MS = 3200;   // slow rotation after sweep
      // 1) Fast sweep — i = 1..N-1 (skip 0 since it's already active)
      let sweepStep = 1;
      const fast = setInterval(() => {
        if (sweepStep >= all.length) {
          clearInterval(fast);
          if (textEl) textEl.classList.remove("is-warming");
          // 2) Slow rotation
          slideTimer = setInterval(() => {
            all[idx].classList.remove("is-active");
            idx = (idx + 1) % all.length;
            all[idx].classList.add("is-active");
            imgCap.textContent = images[idx].caption || "";
          }, REST_MS);
          return;
        }
        all[idx].classList.remove("is-active");
        idx = sweepStep;
        all[idx].classList.add("is-active");
        imgCap.textContent = images[idx].caption || "";
        sweepStep++;
      }, SWEEP_MS);
    }

    async function load() {
      // Cycle 19 v2: dismiss-per-day removed. Page is now a dedicated route the
      // user navigates to intentionally — there's nothing to "dismiss." The card
      // just renders today's phrase whenever the user lands here.
      const today = new Date().toISOString().slice(0, 10);
      let res, data;
      try {
        res = await fetch("/api/phrase-of-the-day");
        if (!res.ok) return;  // silently fail — page degrades gracefully
        data = await res.json();
      } catch { return; }
      if (!data || !data.original_phrase) return;
      dateEl.textContent = data.date || today;
      origEl.textContent = data.original_phrase;
      meanEl.textContent = data.meaning || "";
      // Cycle 18 v2 — backend now sends `images: [{url, caption}, ...]` (12 entries)
      if (Array.isArray(data.images) && data.images.length) {
        startSlideshow(data.images);
      } else {
        slidesEl.parentElement.style.display = "none";
      }
      const translations = data.translations || {};
      const items = Object.entries(translations);
      countEl.textContent = `（${items.length} 种）`;
      listEl.innerHTML = items.map(([variety, text]) => {
        const meta = VARIETIES[variety];
        const label = meta?.label || variety;
        const isErr = !text || text.startsWith("__error__");
        return `<li class="${isErr ? "is-error" : ""}">
          <strong>${escapeHtml(label)}</strong>
          <span>${isErr ? "（生成失败）" : escapeHtml(text)}</span>
        </li>`;
      }).join("");
      // Favorite state — uses readSaved/writeSaved helpers above (F2 viewer wiring).
      const isFav = readSaved().some((s) => s.date === data.date);
      if (favIcon) favIcon.className = isFav ? "fas fa-bookmark" : "far fa-bookmark";
      favBtn.onclick = () => {
        const list = readSaved();
        const idx = list.findIndex((s) => s.date === data.date);
        if (idx >= 0) {
          list.splice(idx, 1);
          if (favIcon) favIcon.className = "far fa-bookmark";
          showToast("已取消收藏", "fa-bookmark");
        } else {
          list.unshift({
            date: data.date, original_phrase: data.original_phrase,
            meaning: data.meaning, translations: data.translations,
          });
          if (favIcon) favIcon.className = "fas fa-bookmark";
          showToast("已收藏到本地", "fa-bookmark");
        }
        writeSaved(list);
        refreshSavedButton();  // F2 — keeps the viewer chip count in sync
      };
      // F2 — show "已收藏 (N)" chip if user has anything saved
      refreshSavedButton();
      // Cycle 19 v2: dismiss button removed; card is always visible on its own page.
      // No `card.hidden` toggling needed.
    }
    // Defer to next tick so VARIETIES + SCENARIOS load first
    setTimeout(load, 50);
  }

  function bindShortcuts() {
    document.addEventListener("keydown", (e) => {
      if (!e.metaKey && !e.ctrlKey && !e.altKey) {
        const isInput = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
        if (!isInput && /^[1-7]$/.test(e.key)) {
          location.hash = ROUTES[+e.key - 1];
        }
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        e.preventDefault();
        rewriteText();
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        clearAll();
        textInput.focus();
      }
    });
  }

  function bindSidebar() {
    sidebarToggle.addEventListener("click", () => {
      document.body.classList.toggle("sidebar-collapsed");
    });
    mobileMenuBtn.addEventListener("click", () => {
      document.body.classList.add("sidebar-open");
    });
    sidebarBackdrop.addEventListener("click", () => {
      document.body.classList.remove("sidebar-open");
    });
    // Stage A5: v3 trigger opens sidebar as right-drawer (same .sidebar-open class).
    const v3MenuBtn = $("#v3-menu-btn");
    if (v3MenuBtn) {
      v3MenuBtn.addEventListener("click", () => {
        document.body.classList.add("sidebar-open");
      });
    }
    // Esc closes the drawer (also useful for the mobile drawer).
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && document.body.classList.contains("sidebar-open")) {
        document.body.classList.remove("sidebar-open");
      }
    });
  }

  function bindWorkbench() {
    textInput.addEventListener("input", updateCharCount);
    clearButton.addEventListener("click", clearAll);
    speakButton.addEventListener("click", speakResult);
    form.addEventListener("submit", rewriteText);
    cmdbarRewrite.addEventListener("click", rewriteText);
    copyButton.addEventListener("click", copyResult);
    downloadButton.addEventListener("click", downloadResult);
    compareToggle.addEventListener("click", toggleCompare);

    favoriteBtn.addEventListener("click", () => {
      if (!lastResult) return;
      const ok = updateHistoryById(lastResult.id, (r) => { r.favorite = !r.favorite; });
      if (!ok) return;
      const rec = loadHistory().find((r) => r.id === lastResult.id);
      syncFavoriteButton(rec);
      showToast(rec && rec.favorite ? "已收藏" : "已取消收藏", rec && rec.favorite ? "fa-star" : "far-star");
    });

    if (compareOtherButton) {
      compareOtherButton.addEventListener("click", () => {
        if (comparePanel.hidden) openCompare();
        else closeCompare();
      });
    }
    if (compareClose) compareClose.addEventListener("click", closeCompare);

    if (explainButton) {
      explainButton.addEventListener("click", () => {
        if (explainDrawer.hidden) openExplain();
        else closeExplain();
      });
    }
    if (explainClose) explainClose.addEventListener("click", closeExplain);

    targetSelect.addEventListener("change", () => {
      const v = targetSelect.value;
      updateVarietyCard();
      applyBackgroundFor(v);
      const meta = VARIETIES[v];
      if (meta && meta.trial) {
        setStatus(STATUS_TEXT.trialNotice(meta.label));
      } else {
        setStatus(STATUS_TEXT.idle);
      }
    });
  }

  // ---------------- init ----------------
  syncSettingsUI();
  applyVersion(SETTINGS.version);
  applySeason(currentSeason());  // v3「随四时」: set [data-season] from month
  detectV3Mode();                 // v3 preview flag from URL / localStorage
  // Stage A1: kick off lazy load (non-blocking — falls back to preset landmarks).
  if (document.body.getAttribute("data-v3") === "on") loadSeasonalLandmarks();
  applyFontScale(SETTINGS.fontScale);
  updateBgModePill();

  bindRouter();
  bindSidebar();
  bindShortcuts();
  bindExampleChips();
  bindSettings();
  bindHistoryTools();
  bindWorkbench();
  bindBatch();
  bindGlossary();
  bindVoiceInput();
  bindContextWizard();   // Cycle 18 — Function 1
  bindDailyPhrase();     // Cycle 18 — Function 2

  Promise.all([loadPresets(), loadScenarios()])
    .then(() => {
      // Default background after presets are loaded.
      applyBackgroundFor(varietyOrder[0]);
      updateVarietyCard();
      clearAll();
      renderAtlas();
      renderHistory();
    })
    .catch((err) => {
      console.error("Init failed:", err);
      setStatus(STATUS_TEXT.loadFail, "error");
    })
    .finally(() => {
      // Cycle 9 fix: boot screen MUST hide regardless of init success.
      setTimeout(() => bootScreen?.classList.add("hidden"), 600);
    });

  // Cycle 9 fix: hard timeout — even if .finally never fires (impossible but defensive),
  // boot screen disappears after 5 seconds so user is never stuck on splash.
  setTimeout(() => bootScreen?.classList.add("hidden"), 5000);
})();
