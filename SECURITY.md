# Security Policy

## Reporting a Vulnerability

If you discover a security issue in NCGA, please **do not** open a public GitHub issue. Instead, email the maintainer directly with:

- A clear description of the issue and where in the code it occurs
- A minimal reproduction (script / curl / payload)
- The version / commit hash you tested against
- Optional: your suggested fix

You should expect an acknowledgement within **3 business days**, and a status update within **14 days**.

We follow [coordinated disclosure](https://en.wikipedia.org/wiki/Coordinated_vulnerability_disclosure): once a fix has shipped, you are welcome (and credited, if you want) to publish your write-up.

## Threat Model

NCGA is a single-user / small-team Chinese rewriting tool. The threat model is:

- **In scope**:
  - API key (`DEEPSEEK_API_KEY`) leakage via process / file / log
  - Public-internet abuse (rate-limit bypass, DoS, token burning)
  - Sensitive user data at rest (rewrites, ratings) leakage
  - XSS via the rewriting result rendering path
  - CSRF on state-changing endpoints
- **Out of scope**:
  - Multi-tenant isolation (NCGA does not have user accounts)
  - Browser-side encryption of localStorage (history / glossary)
  - Defending against compromised LLM provider (DeepSeek API)
  - Supply-chain attacks on `cryptography` / `certifi` deps (pin via `requirements.txt`)

## Hardening Checklist for Production Deployment

| Control | How |
|---|---|
| **TLS** | Always run behind a reverse proxy (Caddy / Nginx / Cloudflare) that terminates HTTPS. NCGA itself only serves HTTP. |
| **Dual-track auth** | Set `NCGA_AUTH_TOKEN=<random 32+ chars>` env. All `POST /api/*` then require one of two tracks. **Browser SPA**: an HMAC-SHA256-signed session cookie (`ncga_sess`, HttpOnly, SameSite=Lax, 30-day max-age) minted on `GET /`; the raw token is never injected into HTML — only a no-secret `<meta name="ncga-auth-mode">` marker. Revocation via an in-memory set + `POST /api/logout`; rotating the token invalidates all cookies at once (it is the signing key). **API / extension / scripts**: `Authorization: Bearer <token>` header. |
| **At-rest encryption of quality store** | Set `NCGA_DATA_KEY=<base64 32 bytes>` env. AES-GCM (NIST-approved). Without this env, NCGA auto-generates a key under `~/.local/share/ncga/data.key` (mode 0600) on first run; *or* falls back to plaintext with a warning if neither is writable. |
| **X-Forwarded-For** | Default: **NOT trusted** (prevents per-IP rate-limit spoofing). Set `NCGA_TRUST_FORWARDED_FOR=true` only when behind a trusted proxy. |
| **CSP** | `script-src 'self' 'nonce-...'` — no `unsafe-inline`. Per-response nonce attached to inline boot script. |
| **HSTS** | `Strict-Transport-Security: max-age=31536000; includeSubDomains` always sent. Take effect when proxy serves over HTTPS. |
| **Rate limit** | 30 requests per IP per minute (default) on the main bucket shared by `POST /api/rewrite` and the Cycle 23 transform endpoints (`POST /api/transform`, `POST /api/transform-stream`, `POST /api/rate-transform` — all behind the same dual-track auth above); 6 batches per IP per minute. These LLM-spending endpoints also count against the per-IP daily cap (`NCGA_DAILY_LLM_CAP_PER_IP`, default 300). Override via `NCGA_RATE_LIMIT_PER_MIN` / `NCGA_BATCH_RATE_LIMIT_PER_MIN`. |
| **Body cap** | 64 KB per request. Override via `NCGA_MAX_BODY_BYTES`. |
| **Path traversal** | `Special:FilePath`-style escapes (`..`, `..%2F`, `%2e%2e/`) all return 404. Static handler resolves and validates `relative_to(STATIC_DIR)`. |
| **Secrets in repo** | `.env` is `.gitignore`d. Quality store is by default in `~/.local/share/ncga/quality.json`, **not** in the repo. |
| **Feedback store is plaintext** | `~/.local/share/ncga/feedback.jsonl` (mode 0600) stores user feedback including the **optional contact email in plaintext** — unlike the quality store, it is *not* AES-GCM encrypted. IPs are salted-SHA-256 hashed before storage, but treat the file itself as sensitive. |

## Generating Strong Tokens

```bash
# 32-byte URL-safe token for NCGA_AUTH_TOKEN
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 32-byte AES-256 key (base64) for NCGA_DATA_KEY
python3 -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

## Cryptography Choices

- **AES-GCM** for quality-store at-rest encryption. Authenticated encryption (confidentiality + tamper-evidence). NIST SP 800-38D.
- **`hmac.compare_digest`** for token comparison (constant-time, defeats timing attacks).
- **`secrets.token_bytes`** for nonces and key generation (CSPRNG).

We deliberately do *not* implement custom crypto; everything is via the standard `cryptography` library or `secrets` / `hmac` from the Python stdlib.
