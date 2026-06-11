// extension/content/content.js
// Renders the rewrite result as a Shadow-DOM-isolated overlay on the active page.
//
// We use Shadow DOM so the host page's CSS cannot bleed into our overlay,
// and our CSS cannot bleed into the host. The accompanying overlay.css is
// declared in manifest's content_scripts; the browser injects it into the
// regular document, BUT we don't actually rely on that file — we attach the
// styles directly into the shadow root below. The CSS file is a fallback if
// shadow attachment is blocked by the page's CSP for any reason.

(function () {
  "use strict";

  // Re-injection guard: chrome.scripting.executeScript may run this file again
  // on tabs that already have it from content_scripts. Without this guard,
  // every reinject would add a duplicate chrome.runtime.onMessage listener.
  if (window.__ncga_content_loaded__) {
    return;
  }
  Object.defineProperty(window, "__ncga_content_loaded__", {
    value: true,
    writable: false,
    configurable: false,
  });

  const HOST_ID = "ncga-overlay-host";
  let hostEl = null;
  let shadowRoot = null;
  let panelEl = null;

  /**
   * ensureHost(anchorRect): create or reuse the overlay host.
   * - anchorRect=null: corner mode (right-bottom of viewport, big window).
   * - anchorRect={pageBottom,pageLeft,width}: selection mode, ABSOLUTE page
   *   coords captured at selection time, positioned just BELOW the selection
   *   bbox, narrower window. Page coords (not viewport) so a scroll during the
   *   LLM round-trip doesn't misplace the popover.
   */
  function ensureHost(anchorRect = null) {
    if (hostEl) {
      // Already exists: only re-position if anchor changed mode
      applyHostPosition(anchorRect);
      return shadowRoot;
    }
    hostEl = document.createElement("div");
    hostEl.id = HOST_ID;
    shadowRoot = hostEl.attachShadow({ mode: "open" });
    applyHostPosition(anchorRect);
    // ⬆ 之前这里有重复的 attachShadow,会 throw DOMException 让 panelEl 留 null,
    //   导致后续 renderResult / renderError 报 "Cannot set properties of null"。
    // Internal CSS — pinned here to avoid host-page interference.
    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; }
      .panel {
        pointer-events: auto;
        background: #FFF8F4;
        color: #1A1A1A;
        border: 1px solid #F0D9CF;
        border-radius: 10px;
        box-shadow: 0 8px 32px rgba(0,0,0,0.15);
        font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Noto Sans SC', sans-serif;
        font-size: 13px;
        line-height: 1.55;
        overflow: hidden;
        max-height: 80vh;
        display: flex;
        flex-direction: column;
      }
      .hdr {
        display: flex;
        align-items: center;
        gap: 0.5em;
        padding: 0.6rem 0.9rem;
        border-bottom: 1px solid #F0D9CF;
      }
      .hdr-title {
        font-weight: 700;
        font-size: 12px;
        color: #1A1A1A;
        flex: 1;
        min-width: 0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .hdr-variety {
        background: #E89BB0;
        color: #FFFFFF;
        font-size: 10px;
        font-weight: 600;
        padding: 0.15em 0.6em;
        border-radius: 999px;
        letter-spacing: 0.04em;
      }
      .hdr-close {
        background: transparent;
        border: 0;
        font-size: 18px;
        color: #5A4844;
        cursor: pointer;
        padding: 0 0.3em;
        line-height: 1;
      }
      .hdr-close:hover { color: #C0392B; }

      .body {
        padding: 0.7rem 0.9rem 0.9rem;
        overflow-y: auto;
        max-height: 56vh;
      }
      .source {
        font-size: 11px;
        color: #5A4844;
        opacity: 0.85;
        margin: 0 0 0.5rem;
        padding: 0 0 0.5rem;
        border-bottom: 1px dashed #F0D9CF;
        max-height: 4em;
        overflow: hidden;
        white-space: pre-wrap;
      }
      .source.is-expanded { max-height: 18em; }
      .source-toggle {
        background: transparent;
        border: 0;
        color: #C95F7D;
        font-size: 10px;
        cursor: pointer;
        padding: 0;
        margin-top: 0.2em;
      }
      .result {
        font-family: 'Noto Serif SC', serif;
        font-size: 15px;
        line-height: 1.75;
        white-space: pre-wrap;
        word-break: break-word;
        margin: 0;
        border-left: 3px solid #E89BB0;
        padding: 0 0 0 0.7rem;
        color: #1A1A1A;
      }
      .loading {
        display: flex;
        align-items: center;
        gap: 0.5em;
        color: #5A4844;
        font-size: 12px;
      }
      .loading::before {
        content: "";
        width: 12px;
        height: 12px;
        border: 2px solid #F0D9CF;
        border-top-color: #E89BB0;
        border-radius: 50%;
        animation: ncga-spin 0.8s linear infinite;
      }
      @keyframes ncga-spin { to { transform: rotate(360deg); } }
      .err {
        color: #C0392B;
        font-size: 12px;
        white-space: pre-wrap;
      }
      .degraded {
        display: inline-block;
        margin: 0 0 0.5rem;
        padding: 0.1em 0.6em;
        border: 1px solid #EED9B8;
        border-radius: 999px;
        background: #FDF3E7;
        color: #B5491D;
        font-size: 10px;
        font-weight: 600;
      }
      .model-chip {
        display: inline-block;
        margin: 0 0 0.5rem;
        padding: 0.1em 0.6em;
        border: 1px solid #E5DAD5;
        border-radius: 999px;
        background: #F6EFEB;
        color: #8A7770;
        font-size: 10px;
        font-weight: 600;
      }
      .degraded + .model-chip { margin-left: 0.4em; }
      .ftr {
        padding: 0.5rem 0.9rem;
        border-top: 1px solid #F0D9CF;
        display: flex;
        gap: 0.5em;
        font-size: 11px;
        color: #5A4844;
      }
      .ftr button {
        background: transparent;
        border: 0;
        color: #5A4844;
        font-size: 11px;
        cursor: pointer;
        padding: 0;
      }
      .ftr button:hover { color: #C95F7D; text-decoration: underline; }
    `;
    shadowRoot.appendChild(style);
    panelEl = document.createElement("div");
    panelEl.className = "panel";
    shadowRoot.appendChild(panelEl);
    document.documentElement.appendChild(hostEl);
    return shadowRoot;
  }

  // tagLabel overrides the header chip text — used by the transform path
  // (mode label instead of a dialect name). null keeps the variety lookup.
  function renderLoading(varietyKey, sourceText, anchorRect = null, tagLabel = null) {
    ensureHost(anchorRect);
    panelEl.textContent = "";
    panelEl.appendChild(buildHeader(varietyKey, true, tagLabel));
    const body = document.createElement("div");
    body.className = "body";
    const src = document.createElement("p");
    src.className = "source";
    src.textContent = sourceText;
    body.appendChild(src);
    const load = document.createElement("div");
    load.className = "loading";
    // 「中英互译中…」拗口 — transform 统一显「处理中…」,模式名在 header chip 里。
    load.textContent = tagLabel ? "处理中…" : "改写中…";
    body.appendChild(load);
    panelEl.appendChild(body);
  }

  // transformMeta = {modeKey, modeLabel, model} for transform results, null for
  // dialect rewrites. modeLabel replaces the dialect name in the header chip;
  // model shows as a muted chip when modeKey === "explain" (the user wants to
  // SEE the per-mode pro-model routing, not just trust it's configured).
  function renderResult(varietyKey, sourceText, result, anchorRect = null, degraded = false, warning = "", transformMeta = null) {
    ensureHost(anchorRect);
    panelEl.textContent = "";
    panelEl.appendChild(buildHeader(varietyKey, true, transformMeta && transformMeta.modeLabel));
    const body = document.createElement("div");
    body.className = "body";
    // Collapsible source
    const src = document.createElement("p");
    src.className = "source";
    src.textContent = sourceText;
    body.appendChild(src);
    if (sourceText && sourceText.length > 80) {
      const toggle = document.createElement("button");
      toggle.className = "source-toggle";
      toggle.textContent = "展开原文";
      toggle.addEventListener("click", () => {
        const exp = src.classList.toggle("is-expanded");
        toggle.textContent = exp ? "收起原文" : "展开原文";
      });
      body.appendChild(toggle);
    }
    if (degraded) {
      // Server fell back to a degraded rewrite — badge it (textContent only).
      // Transforms send degraded:false today; the wiring is shared on purpose.
      const chip = document.createElement("span");
      chip.className = "degraded";
      chip.textContent = warning ? "降级输出 · " + warning : "降级输出";
      body.appendChild(chip);
    }
    if (transformMeta && transformMeta.modeKey === "explain" && transformMeta.model) {
      const mchip = document.createElement("span");
      mchip.className = "model-chip";
      mchip.textContent = transformMeta.model;
      body.appendChild(mchip);
    }
    const out = document.createElement("pre");
    out.className = "result";
    out.textContent = result || "(空)";
    body.appendChild(out);
    panelEl.appendChild(body);
    // Footer with copy button ("复制结果" for transforms — output isn't a 改写)
    const ftr = document.createElement("div");
    ftr.className = "ftr";
    const copyBtn = document.createElement("button");
    const copyLabel = transformMeta ? "复制结果" : "复制改写";
    copyBtn.textContent = copyLabel;
    copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(result);
        copyBtn.textContent = "已复制 ✓";
        setTimeout(() => { copyBtn.textContent = copyLabel; }, 1500);
      } catch (e) {
        copyBtn.textContent = "复制失败:" + e.message;
      }
    });
    ftr.appendChild(copyBtn);
    panelEl.appendChild(ftr);
  }

  function renderError(message, anchorRect = null) {
    ensureHost(anchorRect);
    panelEl.textContent = "";
    panelEl.appendChild(buildHeader("—", true));
    const body = document.createElement("div");
    body.className = "body";
    const err = document.createElement("div");
    err.className = "err";
    err.textContent = "✗ " + (message || "未知错误");
    body.appendChild(err);
    panelEl.appendChild(body);
  }

  function buildHeader(varietyKey, withClose, tagLabel = null) {
    const hdr = document.createElement("div");
    hdr.className = "hdr";
    const title = document.createElement("div");
    title.className = "hdr-title";
    title.textContent = "地道中文";
    hdr.appendChild(title);
    const tag = document.createElement("span");
    tag.className = "hdr-variety";
    tag.textContent = tagLabel || labelForVariety(varietyKey);
    hdr.appendChild(tag);
    if (withClose) {
      const close = document.createElement("button");
      close.className = "hdr-close";
      close.setAttribute("aria-label", "关闭");
      close.textContent = "×";
      close.addEventListener("click", () => destroy());
      hdr.appendChild(close);
    }
    return hdr;
  }

  function labelForVariety(key) {
    return ({
      "standard_putonghua": "普通话",
      "beijing_mandarin": "北京话",
      "dongbei_mandarin": "东北话",
      "sichuan_chongqing_mandarin": "川渝",
      "jianghuai_or_lower_yangtze_mandarin": "江淮",
      "guangdong_mandarin": "广东普",
      "shanghai_mandarin_style": "上海",
      "cantonese_written": "粤书",
      "hokkien_written": "台闽南",
      "minnan_written": "福建闽南",
    })[key] || key || "—";
  }

  // Transform-mode labels — mirror MODE_METADATA in transform.py (same manual
  // sync convention as labelForVariety above).
  function labelForMode(key) {
    return ({
      "polish": "润色",
      "translate": "中英互译",
      "summarize": "总结",
      "explain": "白话解释",
    })[key] || key || "—";
  }

  function destroy() {
    if (hostEl && hostEl.parentNode) hostEl.parentNode.removeChild(hostEl);
    hostEl = null;
    shadowRoot = null;
    panelEl = null;
  }

  /**
   * Position the overlay host. If anchorRect supplied
   * ({pageBottom,pageLeft,width} in ABSOLUTE page coords, captured at
   * selection time), attach absolutely below the selection; otherwise float
   * at bottom-right of viewport. Page coords are baked in by the caller so
   * scrolling between capture and render can't shift the popover.
   */
  function applyHostPosition(anchorRect) {
    if (!hostEl) return;
    overlayMode = anchorRect ? "anchor" : "corner";
    if (anchorRect) {
      const top = anchorRect.pageBottom + 6;             // 6px gap
      let left = anchorRect.pageLeft;
      const popWidth = Math.min(420, Math.max(280, anchorRect.width + 80));
      // Keep popover within the viewport horizontally (clamp against the
      // current horizontal scroll — usually 0).
      const scrollX = window.pageXOffset || 0;
      const maxLeft = scrollX + window.innerWidth - popWidth - 16;
      if (left > maxLeft) left = Math.max(scrollX + 12, maxLeft);
      hostEl.style.cssText = [
        "all: initial",
        "position: absolute",
        `top: ${top}px`,
        `left: ${left}px`,
        `width: ${popWidth}px`,
        "max-width: calc(100vw - 24px)",
        "max-height: 60vh",
        "z-index: 2147483647",
        "pointer-events: none",
        "font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Noto Sans SC', sans-serif",
      ].join(";");
    } else {
      hostEl.style.cssText = [
        "all: initial",
        "position: fixed",
        "right: 16px",
        "bottom: 16px",
        "z-index: 2147483647",
        "width: 380px",
        "max-width: calc(100vw - 32px)",
        "max-height: calc(100vh - 32px)",
        "pointer-events: none",
        "font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Noto Sans SC', sans-serif",
      ].join(";");
    }
  }

  // Mode flag: "corner" (right-click + popup paths) vs "anchor" (selection popover).
  // Mousedown-outside auto-close only applies in anchor mode.
  let overlayMode = "corner";

  // Esc → close overlay (always)
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && hostEl) destroy();
  });

  // Outside click → only auto-close in anchor (selection) mode.
  // In corner mode the user must click × to dismiss — otherwise the popover
  // closes the instant they click anywhere on the page to interact with content.
  // Also delay attaching until next tick so the SAME mousedown that produced
  // the selection (and thus the popover) doesn't immediately trigger close.
  document.addEventListener("mousedown", (e) => {
    if (!hostEl) return;
    if (overlayMode !== "anchor") return;
    if (e.target === hostEl || hostEl.contains(e.target)) return;
    // Defer to next tick so a fresh selection click doesn't kill its own popover
    setTimeout(() => {
      if (hostEl && overlayMode === "anchor") destroy();
    }, 0);
  });

  // ============ selection-anchored auto-rewrite (E7) ============
  // selectionchange + 300ms debounce; if user setting allows + selection ≥3 chars,
  // ask SW to rewrite using the default variety, anchor popover under selection.
  let selectionDebounceTimer = null;
  let lastTriggeredText = "";
  // Monotonic request id: a newer selection (or a destroyed overlay) makes any
  // in-flight response stale, so its render is skipped instead of clobbering.
  let rewriteRequestSeq = 0;

  function onSelectionChange() {
    if (selectionDebounceTimer) clearTimeout(selectionDebounceTimer);
    selectionDebounceTimer = setTimeout(handleSelectionMaybeRewrite, 300);
  }

  async function handleSelectionMaybeRewrite() {
    // User prefs live under "ncga.prefs.v1" (split from ncga.config.v1 so an
    // options.js save can't clobber them). Fallback to the old location for
    // configs written before the split.
    let prefs;
    try {
      const s = await chrome.storage.local.get(["ncga.prefs.v1", "ncga.config.v1"]);
      prefs = s["ncga.prefs.v1"] || s["ncga.config.v1"] || {};
    } catch (_e) { return; }
    // Instant mode only. on_demand (default) never auto-fires on selection —
    // that's the right-click menu's job, and auto-firing here is what collided
    // with it. Back-compat: legacy boolean autoRewriteOnSelection === instant.
    const mode = prefs.mode || (prefs.autoRewriteOnSelection ? "instant" : "on_demand");
    if (mode !== "instant") return;

    const sel = window.getSelection();
    if (!sel || sel.isCollapsed) return;
    const text = sel.toString().trim();
    if (text.length < 2) return;   // 之前 3 → 2,放宽触发阈值
    // Avoid re-firing on same selection (selectionchange fires per micro-movement)
    if (text === lastTriggeredText && hostEl) return;
    lastTriggeredText = text;

    const range = sel.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    if (!rect || (rect.width === 0 && rect.height === 0)) return;
    // Capture the anchor in ABSOLUTE page coords NOW — getBoundingClientRect
    // is viewport-relative, so converting at render time (after the LLM
    // round-trip) would misplace the popover if the user scrolled meanwhile.
    const anchor = {
      pageBottom: (window.pageYOffset || 0) + rect.bottom,
      pageLeft: (window.pageXOffset || 0) + rect.left,
      width: rect.width,
    };

    // Default action: "variety:<key>" | "mode:<key>" (defaultAction pref,
    // Cycle 23). Absent/legacy values fall back to defaultVariety — pre-split
    // installs keep behaving exactly as before.
    const action = typeof prefs.defaultAction === "string" ? prefs.defaultAction : "";
    let variety = null;
    let modeKey = null;
    if (action.startsWith("mode:")) {
      modeKey = action.slice("mode:".length);
    } else if (action.startsWith("variety:")) {
      variety = action.slice("variety:".length);
    } else {
      variety = prefs.defaultVariety || "shanghai_mandarin_style";
    }
    const tagLabel = modeKey ? labelForMode(modeKey) : null;
    const requestId = ++rewriteRequestSeq;
    // Render loading immediately at anchor
    renderLoading(variety || modeKey, text, anchor, tagLabel);

    try {
      const res = await chrome.runtime.sendMessage(
        modeKey
          ? { type: "ncga:rewrite-from-selection", mode: modeKey, text }
          : { type: "ncga:rewrite-from-selection", varietyKey: variety, text },
      );
      // Stale guard: a newer selection fired, or the user closed the overlay
      // (Esc / outside click / ×) while this request was in flight.
      if (requestId !== rewriteRequestSeq || !hostEl) return;
      if (res && res.ok) {
        renderResult(
          variety || modeKey, text, res.result || "", anchor, !!res.degraded, res.warning || "",
          modeKey
            ? { modeKey, modeLabel: res.modeLabel || tagLabel, model: res.model || "" }
            : null,
        );
      } else {
        renderError((res && res.error) || "未知错误", anchor);
      }
    } catch (e) {
      if (requestId !== rewriteRequestSeq || !hostEl) return;
      renderError(String(e && e.message || e), anchor);
    }
  }

  document.addEventListener("selectionchange", onSelectionChange);

  // ============ message bridge with background SW ============
  // Transform overlay messages carry {mode, modeLabel, model} instead of
  // {varietyKey}; the header chip shows the mode label in that case.
  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || !msg.type) return;
    if (msg.type === "ncga:overlay-loading") {
      const tagLabel = msg.mode ? (msg.modeLabel || labelForMode(msg.mode)) : null;
      renderLoading(msg.varietyKey, msg.text || "", null, tagLabel);
    } else if (msg.type === "ncga:overlay-result") {
      renderResult(
        msg.varietyKey, msg.text || "", msg.result || "", null, !!msg.degraded, msg.warning || "",
        msg.mode
          ? { modeKey: msg.mode, modeLabel: msg.modeLabel || labelForMode(msg.mode), model: msg.model || "" }
          : null,
      );
    } else if (msg.type === "ncga:overlay-error") {
      renderError(msg.error || "未知错误");
    }
  });
})();
