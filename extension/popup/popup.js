// extension/popup/popup.js
// Wires up the popup UI. Talks to the background SW via runtime messages.
// Does NOT do crypto here — passphrase is forwarded to the SW which decrypts.

const $ = (sel) => document.querySelector(sel);

const statusEl = $("#status-line");
const unlockPanel = $("#unlock-panel");
const rewritePanel = $("#rewrite-panel");
const passInput = $("#passphrase");
const unlockBtn = $("#unlock-btn");
const unlockErr = $("#unlock-err");
const textArea = $("#text");
const varietySel = $("#variety");
const rewriteBtn = $("#rewrite-btn");
const resultEl = $("#result");
const openOptions = $("#open-options");
const lockBtn = $("#lock-btn");
const modeOpts = document.querySelectorAll(".mode-opt");
const modeHint = $("#mode-hint");

const CONFIG_KEY = "ncga.config.v1";

// Two mutually-exclusive on-page modes (replaces the old auto-rewrite checkbox,
// which collided with the right-click menu by layering on top of it):
//   on_demand (点选 / On-Demand): right-click → pick a variety → corner overlay.
//                                 Selection NEVER auto-fires. Default.
//   instant   (即时 / Instant):   select text → auto popover under the selection
//                                 using the default variety. No clicks.
const MODE_HINTS = {
  on_demand: "点选:选中文字 → 右键「改写为」→ 自己挑方言。选中不会自动弹窗。",
  instant: "即时:选中文字(≥2 字)→ 立刻在选区下方弹出改写,用下面选的默认方言。",
};

function _migrateMode(cfg) {
  // Back-compat: old config used a boolean autoRewriteOnSelection.
  if (cfg.mode === "on_demand" || cfg.mode === "instant") return cfg.mode;
  if (cfg.autoRewriteOnSelection) return "instant";
  return "on_demand";
}

function _paintMode(mode) {
  modeOpts.forEach((b) => {
    const on = b.dataset.mode === mode;
    b.setAttribute("aria-checked", on ? "true" : "false");
  });
  modeHint.textContent = MODE_HINTS[mode] || "";
}

// 即时 mode needs content.js live in the active tab to hear selectionchange.
// Static content_scripts only cover pages loaded after the extension reload, so
// ask the SW to inject into the current tab (idempotent — content.js guards
// against double-injection). No-op / harmless on un-injectable pages.
async function ensureInjectedFor(mode) {
  if (mode !== "instant") return;
  try {
    await chrome.runtime.sendMessage({ type: "ncga:ensure-content-injected" });
  } catch (_e) { /* un-injectable page (chrome://, store) — ignore */ }
}

async function loadModeSwitch() {
  try {
    const s = await chrome.storage.local.get(CONFIG_KEY);
    const cfg = s[CONFIG_KEY] || {};
    const mode = _migrateMode(cfg);
    _paintMode(mode);
    if (cfg.defaultVariety) varietySel.value = cfg.defaultVariety;
    await ensureInjectedFor(mode);
  } catch (_e) {
    _paintMode("on_demand");
  }
}

modeOpts.forEach((btn) => {
  btn.addEventListener("click", async () => {
    const mode = btn.dataset.mode;
    _paintMode(mode);
    const s = await chrome.storage.local.get(CONFIG_KEY);
    const cfg = s[CONFIG_KEY] || {};
    cfg.mode = mode;
    delete cfg.autoRewriteOnSelection; // drop the legacy flag
    await chrome.storage.local.set({ [CONFIG_KEY]: cfg });
    // Switching to 即时 right now: inject into the current tab so it works
    // immediately, without making the user reload the page.
    await ensureInjectedFor(mode);
  });
});

varietySel.addEventListener("change", async () => {
  // When user changes variety in popup, persist as defaultVariety for
  // selection auto-rewrite path. No-op if config not yet saved (just stores it).
  const s = await chrome.storage.local.get(CONFIG_KEY);
  const cfg = s[CONFIG_KEY] || {};
  cfg.defaultVariety = varietySel.value;
  await chrome.storage.local.set({ [CONFIG_KEY]: cfg });
});

function showStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = kind || "";
}

async function refresh() {
  // 1) Are we configured at all?
  const status = await chrome.runtime.sendMessage({ type: "ncga:status" });
  if (!status || !status.configured) {
    showStatus("未配置 — 请先点「设置」填服务器与 token。", "warn");
    unlockPanel.classList.add("hidden");
    rewritePanel.classList.add("hidden");
    lockBtn.classList.add("hidden");
    return;
  }
  // 2) Is the token unlocked in session?
  const lockState = await chrome.runtime.sendMessage({ type: "ncga:locked" });
  if (lockState && lockState.unlocked) {
    showStatus("已就绪 · " + status.serverUrl, "ok");
    unlockPanel.classList.add("hidden");
    rewritePanel.classList.remove("hidden");
    lockBtn.classList.remove("hidden");
    textArea.focus();
  } else {
    showStatus("Token 已锁定 — 输 passphrase 解锁(关浏览器才会锁)。", "warn");
    unlockPanel.classList.remove("hidden");
    rewritePanel.classList.add("hidden");
    lockBtn.classList.add("hidden");
    passInput.focus();
  }
}

unlockBtn.addEventListener("click", async () => {
  unlockErr.textContent = "";
  const pw = passInput.value;
  if (!pw) { unlockErr.textContent = "passphrase 不可空"; return; }
  unlockBtn.disabled = true;
  try {
    const res = await chrome.runtime.sendMessage({ type: "ncga:unlock", passphrase: pw });
    if (res && res.ok) {
      passInput.value = "";
      await refresh();
    } else {
      unlockErr.textContent = "解锁失败:" + (res && res.error || "passphrase 错误?");
    }
  } finally {
    unlockBtn.disabled = false;
  }
});

passInput.addEventListener("keydown", (e) => { if (e.key === "Enter") unlockBtn.click(); });

rewriteBtn.addEventListener("click", async () => {
  const text = textArea.value.trim();
  if (!text) return;
  const varietyKey = varietySel.value;
  rewriteBtn.disabled = true;
  resultEl.textContent = "改写中…";
  try {
    const res = await chrome.runtime.sendMessage({
      type: "ncga:rewrite-from-popup",
      varietyKey,
      text,
    });
    if (res && res.ok) {
      resultEl.textContent = res.result || "(空)";
    } else {
      resultEl.textContent = "✗ " + (res && res.error || "未知错误");
    }
  } catch (e) {
    resultEl.textContent = "✗ " + (e && e.message || e);
  } finally {
    rewriteBtn.disabled = false;
  }
});

openOptions.addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

lockBtn.addEventListener("click", async (e) => {
  e.preventDefault();
  await chrome.storage.session.remove("ncga.session.v1");
  await refresh();
});

refresh();
loadModeSwitch();
