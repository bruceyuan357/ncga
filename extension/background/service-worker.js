// extension/background/service-worker.js
// Type: module (per manifest)
//
// Responsibilities:
//   1. Build the right-click context menu with all 10 NCGA varieties on install
//   2. On menu click, ask the active tab's content script to grab the selection
//      and post it back here for the rewrite request
//   3. Make the /api/rewrite call to the configured NCGA server with auth
//   4. Forward the result to the content script for overlay rendering
//
// Auth model:
//   - chrome.storage.local stores { serverUrl, encryptedToken }
//   - We DO NOT cache the decrypted token here. The decrypted token lives in
//     a session memory cache that auto-clears after IDLE_CLEAR_MS of inactivity.
//   - Decryption requires a passphrase the user enters in Options. If the
//     passphrase isn't cached yet, the service worker prompts via the popup.

import { decrypt } from "../lib/crypto.js";

const STORAGE_KEY = "ncga.config.v1";
const SESSION_KEY = "ncga.session.v1";  // chrome.storage.session
const IDLE_CLEAR_MS = 30 * 60 * 1000;   // 30 min token cache TTL

// 10 varieties hard-coded for context menu titles. Sourced from
// native_chinese_assistant/presets.py; kept in sync manually for now.
// Future improvement: fetch /api/presets on install + cache.
const VARIETIES = [
  { key: "standard_putonghua",                       label: "标准普通话" },
  { key: "beijing_mandarin",                         label: "北京话" },
  { key: "dongbei_mandarin",                         label: "东北话" },
  { key: "sichuan_chongqing_mandarin",               label: "川渝话" },
  { key: "jianghuai_or_lower_yangtze_mandarin",      label: "江淮话" },
  { key: "guangdong_mandarin",                       label: "广东普通话" },
  { key: "shanghai_mandarin_style",                  label: "上海话风格" },
  { key: "cantonese_written",                        label: "粤语书面语" },
  { key: "hokkien_written",                          label: "台湾闽南语" },
  { key: "minnan_written",                           label: "福建闽南语" },
];

// ============ context menu ============

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "ncga-root",
      title: "改写为(地道中文)",
      contexts: ["selection"],
    });
    for (const v of VARIETIES) {
      chrome.contextMenus.create({
        id: "ncga-variety-" + v.key,
        parentId: "ncga-root",
        title: v.label,
        contexts: ["selection"],
      });
    }
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (!info.menuItemId || !info.menuItemId.startsWith("ncga-variety-")) return;
  const varietyKey = info.menuItemId.slice("ncga-variety-".length);
  const text = (info.selectionText || "").trim();
  if (!text || !tab || !tab.id) return;
  await handleRewriteRequest(tab.id, varietyKey, text);
});

// ============ message handlers ============

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // Popup-initiated rewrite (user types in popup, picks variety)
  if (msg && msg.type === "ncga:rewrite-from-popup") {
    handleRewriteRequest(
      (sender.tab && sender.tab.id) || null,
      msg.varietyKey,
      msg.text,
    ).then(
      (result) => sendResponse({ ok: true, result }),
      (err) => sendResponse({ ok: false, error: String(err && err.message || err) }),
    );
    return true; // async response
  }
  // Options-initiated config save (after encrypting token there)
  if (msg && msg.type === "ncga:config-saved") {
    sendResponse({ ok: true });
    return true;
  }
  // Popup asks "are we configured?" (does NOT need passphrase)
  if (msg && msg.type === "ncga:status") {
    chrome.storage.local.get(STORAGE_KEY).then((store) => {
      const c = store[STORAGE_KEY] || {};
      sendResponse({
        ok: true,
        configured: !!(c.serverUrl && c.encryptedToken),
        serverUrl: c.serverUrl || null,
      });
    });
    return true;
  }
  // Popup or options posts passphrase to unlock token into session cache
  if (msg && msg.type === "ncga:unlock") {
    unlockTokenWithPassphrase(msg.passphrase).then(
      () => sendResponse({ ok: true }),
      (err) => sendResponse({ ok: false, error: String(err && err.message || err) }),
    );
    return true;
  }
  // Popup asks "is token unlocked in session?"
  if (msg && msg.type === "ncga:locked") {
    chrome.storage.session.get(SESSION_KEY).then((s) => {
      const ses = s[SESSION_KEY];
      const unlocked = !!(ses && ses.token && ses.expiresAt > Date.now());
      sendResponse({ ok: true, unlocked });
    });
    return true;
  }
});

// ============ rewrite pipeline ============

async function handleRewriteRequest(tabId, varietyKey, text) {
  const cfg = await getConfig();
  if (!cfg.serverUrl) {
    return notifyError(tabId, "请先在扩展 Options 中填写服务器地址。");
  }
  const token = await getCachedToken();
  if (!token) {
    return notifyError(tabId, "Token 未解锁,点扩展图标输入 passphrase 解锁。");
  }
  if (tabId) {
    await sendToTab(tabId, { type: "ncga:overlay-loading", varietyKey, text });
  }
  try {
    const result = await callRewriteAPI(cfg.serverUrl, token, varietyKey, text);
    if (tabId) {
      await sendToTab(tabId, { type: "ncga:overlay-result", varietyKey, text, result });
    }
    return result;
  } catch (e) {
    const msg = (e && e.message) || String(e);
    if (tabId) await sendToTab(tabId, { type: "ncga:overlay-error", error: msg });
    throw e;
  }
}

async function callRewriteAPI(serverUrl, token, varietyKey, text) {
  const url = serverUrl.replace(/\/$/, "") + "/api/rewrite";
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": "Bearer " + token,
    },
    body: JSON.stringify({ target_variety: varietyKey, text }),
  });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).error || ""; } catch (_e) {}
    throw new Error("HTTP " + res.status + (detail ? " · " + detail : ""));
  }
  const data = await res.json();
  return data.text || data.result || "";
}

async function sendToTab(tabId, msg) {
  try {
    await chrome.tabs.sendMessage(tabId, msg);
  } catch (e) {
    // tab may have navigated; swallow
  }
}

async function notifyError(tabId, text) {
  if (tabId) await sendToTab(tabId, { type: "ncga:overlay-error", error: text });
  throw new Error(text);
}

// ============ config + token cache ============

async function getConfig() {
  const store = await chrome.storage.local.get(STORAGE_KEY);
  return store[STORAGE_KEY] || {};
}

async function getCachedToken() {
  const s = await chrome.storage.session.get(SESSION_KEY);
  const ses = s[SESSION_KEY];
  if (!ses || !ses.token || ses.expiresAt < Date.now()) return null;
  // Refresh idle timer on every read
  ses.expiresAt = Date.now() + IDLE_CLEAR_MS;
  await chrome.storage.session.set({ [SESSION_KEY]: ses });
  return ses.token;
}

async function unlockTokenWithPassphrase(passphrase) {
  if (!passphrase) throw new Error("passphrase required");
  const cfg = await getConfig();
  if (!cfg.encryptedToken) throw new Error("no encrypted token in storage");
  const token = await decrypt(cfg.encryptedToken, passphrase);
  if (!token) throw new Error("decryption returned empty");
  await chrome.storage.session.set({
    [SESSION_KEY]: { token, expiresAt: Date.now() + IDLE_CLEAR_MS },
  });
}
