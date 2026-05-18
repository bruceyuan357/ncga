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
    showStatus("Token 已锁定 — 输 passphrase 解锁(30 分钟自动锁)。", "warn");
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
