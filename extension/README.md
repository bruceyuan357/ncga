# 地道中文 — Chrome Extension

Right-click any text on any page → 改写为「上海话 / 广州普通话 / 川渝话 / ...」 — routed through your local NCGA server.

## Status

**v0.1.0** — scaffold complete, awaiting user install + live debug session (per Cycle 22 Stage B plan, see `~/.claude/projects/-Users-bruce-NCGA/memory/cycle_20_21_redo_log.md`).

## Architecture

```
extension/
├── manifest.json          Manifest V3, contextMenus + storage + scripting perms,
│                          host-permissions limited to localhost:8000/127.0.0.1:8000
├── icons/                 16/32/48/128 PNG placeholders (replace before publish)
├── background/
│   └── service-worker.js  Context menu setup + message router + auth header builder
├── content/
│   ├── content.js         Selection capture + result overlay (Shadow DOM-isolated)
│   └── overlay.css        Styles loaded into shadow root
├── popup/
│   ├── popup.html         Quick variety picker + status + open-options link
│   ├── popup.css
│   └── popup.js
├── options/
│   ├── options.html       Server URL + bearer token entry + lock/unlock
│   ├── options.css
│   └── options.js
└── lib/
    └── crypto.js          AES-GCM token encryption via Web Crypto (PBKDF2-250k)
```

## Why localhost-only

Default `host_permissions` only allow `http://localhost:8000/*` and `http://127.0.0.1:8000/*`. This is intentional:

- During development you run NCGA at `python3 app.py`
- The extension never talks to a third party
- Your `NCGA_AUTH_TOKEN` lives only in encrypted browser storage (AES-GCM via PBKDF2-derived key from a passphrase the user sets in Options)
- To deploy against a remote NCGA, add your URL to `host_permissions` in `manifest.json` and reinstall the extension

## Install (development / unpacked)

1. Open Chrome → `chrome://extensions`
2. Top-right toggle → **Developer mode** ON
3. Click **Load unpacked** → select this `extension/` folder
4. Pin the extension to the toolbar (puzzle icon → pin)
5. Click the extension icon → **Settings** → enter:
   - Server URL: `http://localhost:8000`
   - Auth token: paste your `NCGA_AUTH_TOKEN` from server `.env`
   - Passphrase: a memorable phrase used only to encrypt the token in `chrome.storage.local` (does not leave the device)
6. Click **Save & Lock** — token gets AES-GCM-encrypted at rest

## Use

1. Highlight any text on any page (English / 中文 / 日文 ...)
2. Right-click → **改写为** → pick a variety
3. Overlay appears with the rewrite (Shadow DOM, no style bleed)
4. ESC to dismiss

## Develop

- Edit any file → Chrome → `chrome://extensions` → click reload icon under NCGA card
- Service worker logs: `chrome://extensions` → "Service worker" link under NCGA
- Content script logs: open devtools on any page, console will show overlay logs

## Build progression (per Cycle 22 Stage B plan)

| Step | What | Commit |
|---|---|---|
| E1 | manifest + folder skeleton + icons | this commit |
| E2 | background service worker | next |
| E3 | popup UI | … |
| E4 | content script + overlay | … |
| E5 | AES-GCM token storage | … |
| E6 | install verification doc | … |

After E6, awaiting user time for live install + debug session.
