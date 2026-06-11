// extension/options/options.js
// Saves { serverUrl, encryptedToken } into chrome.storage.local after encrypting
// the bearer with the user's passphrase via Web Crypto AES-GCM.

import { encrypt } from "../lib/crypto.js";

const STORAGE_KEY = "ncga.config.v1";

const $ = (s) => document.querySelector(s);

const serverInput = $("#server-url");
const tokenInput = $("#token");
const passInput = $("#passphrase");
const pass2Input = $("#passphrase2");
const saveBtn = $("#save-btn");
const saveMsg = $("#save-msg");
const statusOut = $("#status-out");
const clearBtn = $("#clear-btn");

function setMsg(text, kind) {
  saveMsg.textContent = text;
  saveMsg.className = "msg " + (kind || "");
}

async function loadStatus() {
  const store = await chrome.storage.local.get(STORAGE_KEY);
  const c = store[STORAGE_KEY] || {};
  const lines = [];
  lines.push("serverUrl:        " + (c.serverUrl || "(unset)"));
  lines.push("encryptedToken:   " + (c.encryptedToken ? c.encryptedToken.slice(0, 28) + "…" : "(unset)"));
  lines.push("token blob len:   " + (c.encryptedToken ? c.encryptedToken.length + " chars" : "—"));
  lines.push("saved at:         " + (c.savedAt ? new Date(c.savedAt).toISOString() : "—"));
  statusOut.textContent = lines.join("\n");
  // Hydrate URL into the input if blank
  if (c.serverUrl && !serverInput.value) serverInput.value = c.serverUrl;
}

saveBtn.addEventListener("click", async () => {
  setMsg("", "");
  const serverUrl = serverInput.value.trim().replace(/\/$/, "");
  const token = tokenInput.value;
  const pw = passInput.value;
  const pw2 = pass2Input.value;

  if (!serverUrl) return setMsg("✗ 服务器 URL 必填", "err");
  if (!/^https?:\/\//i.test(serverUrl)) return setMsg("✗ 服务器 URL 需以 http:// 或 https:// 开头", "err");
  if (!token) return setMsg("✗ token 必填", "err");
  if (!pw) return setMsg("✗ passphrase 必填", "err");
  if (pw.length < 6) return setMsg("✗ passphrase 至少 6 个字符", "err");
  if (pw !== pw2) return setMsg("✗ 两次 passphrase 不一致", "err");

  saveBtn.disabled = true;
  try {
    const encryptedToken = await encrypt(token, pw);
    // Merge instead of replace: user prefs now live under "ncga.prefs.v1",
    // but merge-write anyway so a save can never clobber stray legacy fields.
    const store = await chrome.storage.local.get(STORAGE_KEY);
    const cur = store[STORAGE_KEY] || {};
    await chrome.storage.local.set({
      [STORAGE_KEY]: { ...cur, serverUrl, encryptedToken, savedAt: Date.now() },
    });
    // 立刻锁掉旧 session(强制按新 token 重解锁)
    await chrome.storage.session.remove("ncga.session.v1");
    // Wipe sensitive inputs
    tokenInput.value = "";
    passInput.value = "";
    pass2Input.value = "";
    setMsg("✓ 已加密保存。打开扩展弹窗输 passphrase 解锁即可使用。", "ok");
    await loadStatus();
  } catch (e) {
    setMsg("✗ 保存失败:" + (e && e.message || e), "err");
  } finally {
    saveBtn.disabled = false;
  }
});

clearBtn.addEventListener("click", async () => {
  if (!confirm("清空所有保存的配置(URL + 加密 token + session)?")) return;
  await chrome.storage.local.remove(STORAGE_KEY);
  await chrome.storage.session.remove("ncga.session.v1");
  serverInput.value = "http://localhost:8000";
  setMsg("已清空。", "ok");
  await loadStatus();
});

loadStatus();
