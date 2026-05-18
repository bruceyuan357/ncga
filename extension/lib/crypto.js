// extension/lib/crypto.js
// AES-GCM token encryption via Web Crypto API.
// Key is derived via PBKDF2 (250k iters, SHA-256) from a user-chosen passphrase.
// Passphrase is NEVER stored — only the salted derived key gets used in memory,
// and the encrypted blob (+ salt + iv) lives in chrome.storage.local.
//
// API surface (callable from popup/options/service-worker):
//   await ncgaCrypto.encrypt(plaintext: string, passphrase: string) -> stored blob string
//   await ncgaCrypto.decrypt(blob: string, passphrase: string)     -> plaintext string
//   ncgaCrypto.generateSalt() -> Uint8Array(16)
//
// Stored blob format (base64-url, single string):
//   "v1." + b64u(salt 16B) + "." + b64u(iv 12B) + "." + b64u(ciphertext)
// "v1." prefix lets us roll the scheme later without breaking old saves.

const PBKDF2_ITERS = 250000;
const SALT_BYTES = 16;
const IV_BYTES = 12;
const SCHEME_PREFIX = "v1.";

function b64uEncode(bytes) {
  let bin = "";
  const len = bytes.byteLength;
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  for (let i = 0; i < len; i++) bin += String.fromCharCode(view[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64uDecode(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  while (s.length % 4) s += "=";
  const bin = atob(s);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

async function deriveKey(passphrase, salt) {
  const enc = new TextEncoder();
  const keyMaterial = await crypto.subtle.importKey(
    "raw",
    enc.encode(passphrase),
    { name: "PBKDF2" },
    false,
    ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    {
      name: "PBKDF2",
      salt,
      iterations: PBKDF2_ITERS,
      hash: "SHA-256",
    },
    keyMaterial,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

function generateSalt() {
  return crypto.getRandomValues(new Uint8Array(SALT_BYTES));
}

async function encrypt(plaintext, passphrase) {
  if (typeof plaintext !== "string" || !plaintext) throw new Error("plaintext required");
  if (typeof passphrase !== "string" || !passphrase) throw new Error("passphrase required");
  const salt = generateSalt();
  const iv = crypto.getRandomValues(new Uint8Array(IV_BYTES));
  const key = await deriveKey(passphrase, salt);
  const enc = new TextEncoder();
  const ct = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    key,
    enc.encode(plaintext),
  );
  return (
    SCHEME_PREFIX +
    b64uEncode(salt) + "." +
    b64uEncode(iv) + "." +
    b64uEncode(new Uint8Array(ct))
  );
}

async function decrypt(blob, passphrase) {
  if (typeof blob !== "string" || !blob.startsWith(SCHEME_PREFIX)) {
    throw new Error("unknown blob format");
  }
  const parts = blob.slice(SCHEME_PREFIX.length).split(".");
  if (parts.length !== 3) throw new Error("malformed blob");
  const salt = b64uDecode(parts[0]);
  const iv = b64uDecode(parts[1]);
  const ct = b64uDecode(parts[2]);
  const key = await deriveKey(passphrase, salt);
  const pt = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv },
    key,
    ct,
  );
  return new TextDecoder().decode(pt);
}

// Expose as ES module (manifest declares background as "type": "module").
// Popup/options can `import { encrypt, decrypt } from "../lib/crypto.js"`.
export { encrypt, decrypt, generateSalt };
