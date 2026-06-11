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

const CONFIG_KEY = "ncga.config.v1";  // {serverUrl, encryptedToken, savedAt} — owned by options.js
const PREFS_KEY = "ncga.prefs.v1";    // {mode, defaultVariety} — owned by popup; read by content.js

// Read user prefs. One-time migration: prefs used to live inside CONFIG_KEY,
// which let an options.js save clobber them — they now have their own key.
async function loadPrefs() {
  const s = await chrome.storage.local.get([PREFS_KEY, CONFIG_KEY]);
  if (s[PREFS_KEY]) return s[PREFS_KEY];
  const old = s[CONFIG_KEY] || {};
  const prefs = {};
  if (old.mode) prefs.mode = old.mode;
  if (old.defaultVariety) prefs.defaultVariety = old.defaultVariety;
  if (old.autoRewriteOnSelection) prefs.autoRewriteOnSelection = old.autoRewriteOnSelection;
  if (Object.keys(prefs).length) {
    await chrome.storage.local.set({ [PREFS_KEY]: prefs });
  }
  return prefs;
}

async function savePrefs(patch) {
  const cur = await loadPrefs();
  const next = { ...cur, ...patch };
  if (patch.mode) delete next.autoRewriteOnSelection; // drop the legacy flag
  await chrome.storage.local.set({ [PREFS_KEY]: next });
}

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

function _migrateMode(prefs) {
  // Back-compat: old config used a boolean autoRewriteOnSelection.
  if (prefs.mode === "on_demand" || prefs.mode === "instant") return prefs.mode;
  if (prefs.autoRewriteOnSelection) return "instant";
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
    const prefs = await loadPrefs();
    const mode = _migrateMode(prefs);
    _paintMode(mode);
    if (prefs.defaultVariety) varietySel.value = prefs.defaultVariety;
    await ensureInjectedFor(mode);
  } catch (_e) {
    _paintMode("on_demand");
  }
}

modeOpts.forEach((btn) => {
  btn.addEventListener("click", async () => {
    const mode = btn.dataset.mode;
    _paintMode(mode);
    await savePrefs({ mode });
    // Switching to 即时 right now: inject into the current tab so it works
    // immediately, without making the user reload the page.
    await ensureInjectedFor(mode);
  });
});

varietySel.addEventListener("change", async () => {
  // When user changes variety in popup, persist as defaultVariety for
  // selection auto-rewrite path.
  await savePrefs({ defaultVariety: varietySel.value });
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
      resultEl.textContent = "";
      if (res.degraded) {
        // textContent only — no innerHTML.
        const chip = document.createElement("span");
        chip.className = "degraded-chip";
        chip.textContent = res.warning ? "降级输出 · " + res.warning : "降级输出";
        resultEl.appendChild(chip);
      }
      resultEl.appendChild(document.createTextNode(res.result || "(空)"));
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
