# 地道中文 — Chrome Extension

Right-click any text on any page → 改写为「上海话 / 广州普通话 / 川渝话 / ...」 — routed through your local NCGA server.

## Status

**v0.1.0** — debugged; live install on Bruce's Chrome done (Cycle 22 Stage B).

## Architecture

```
extension/
├── manifest.json          Manifest V3, contextMenus + storage + scripting +
│                          activeTab perms, host-permissions limited to
│                          localhost:8000/127.0.0.1:8000
├── icons/                 16/32/48/128 PNG, 5-petal cherry blossom
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

## Selection-popover mode (即时)

The popup has a two-position mode switch:

- **点选 / On-Demand** (default) — selection never auto-fires; you pick a
  variety from the right-click menu and the result shows in a corner overlay.
- **即时 / Instant** — selecting ≥2 characters auto-rewrites with the default
  variety and shows a popover anchored just below the selection. The anchor is
  captured in absolute page coords at selection time, so scrolling during the
  LLM round-trip doesn't misplace it; a request-generation guard drops stale
  responses if you select something else (or close the popover) mid-flight.

Results the server marks `degraded` get a small 「降级输出」 badge in both the
popup and the overlay.

## Storage layout

Two separate `chrome.storage.local` keys, so writers never clobber each other:

- `ncga.config.v1` — `{serverUrl, encryptedToken, savedAt}`, written only by
  the Options page.
- `ncga.prefs.v1` — `{mode, defaultVariety}`, written only by the popup, read
  by the content script (with a fallback read of the old in-config location
  for pre-split installs).

The decrypted token lives in `chrome.storage.session` (`ncga.session.v1`) and
clears when the browser session ends.

## Develop

- Edit any file → Chrome → `chrome://extensions` → click reload icon under NCGA card
- Service worker logs: `chrome://extensions` → "Service worker" link under NCGA
- Content script logs: open devtools on any page, console will show overlay logs
