"""WSGI application: routing, rate limiting, body cap, security headers."""

from __future__ import annotations

import json
import logging
import logging.config
import mimetypes
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path

from native_chinese_assistant.feedback import QualityStore
from native_chinese_assistant.presets import parse_scenario, preset_options, scenario_options
from native_chinese_assistant.rewrite import (
    MAX_INPUT_CHARS,
    RewriteError,
    RewriteService,
    load_dotenv,
    parse_variety,
)

logger = logging.getLogger("ncga.web")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = (BASE_DIR / "static").resolve()

DEFAULT_RATE_LIMIT_PER_MIN = 30
DEFAULT_BATCH_RATE_LIMIT_PER_MIN = 6  # batch is heavy; 6 batches/min/IP is generous
DEFAULT_CHARACTERIZE_RATE_LIMIT_PER_MIN = 6  # Cycle 18 — wizard is opt-in; tight cap
# Cycle 18 v2: aggregate per-IP daily ceiling on LLM-spending endpoints. Shared
# bucket across rewrite / batch / rate / explain / characterize / phrase-of-the-day.
# 300 calls is generous for a single user but caps a runaway script at <$1/day.
DEFAULT_DAILY_LLM_CAP_PER_IP = 300
DEFAULT_MAX_BODY_BYTES = 64 * 1024  # 64 KB — bumped to fit batch inputs (≤100 items × short text)
# Cycle 18 v2 (per A5): bumped 1KB → 4KB so users can paste a longer background blurb.
# Still much smaller than the main rewrite cap, since recipient + mood are bounded fields.
DEFAULT_CHARACTERIZE_MAX_BODY_BYTES = 4 * 1024  # 4 KB
# Cycle 18 v2 (per A6): bumped 120 → 240 so users can write a real sentence per field.
CHARACTERIZE_FIELD_MAX_CHARS = 240
# Cycle 20: in-app reflection form (POST /api/feedback). Append-only JSONL.
DEFAULT_FEEDBACK_RATE_LIMIT_PER_MIN = 5  # tight: honest users only need a few
DEFAULT_FEEDBACK_MAX_BODY_BYTES = 8 * 1024  # 8 KB — note ≤800 chars + chips
FEEDBACK_NOTE_MAX_CHARS = 800
FEEDBACK_CONTACT_MAX_CHARS = 120
FEEDBACK_CHIP_MAX = 10
FEEDBACK_CHIP_LEN = 24
# Cycle 20: strip C0 + DEL control chars before persisting any user-supplied
# text to the feedback JSONL. Two reasons: (a) defense against terminal
# escape-sequence injection when an operator tails the file; (b) consistency
# so logs from different OSes look the same.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _const_eq(a: str, b: str) -> bool:
    """Constant-time string comparison to defeat timing attacks on tokens."""
    import hmac

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _check_auth(environ: dict) -> bool:
    """Cycle 20: dual-track auth.

    A POST /api/* is allowed if EITHER:
      1. Authorization: Bearer <NCGA_AUTH_TOKEN>  — the "API-key path" used by
         scripts / Chrome extension / curl. Token stays in .env; operator hands
         it out manually.
      2. Cookie: ncga_sess=<HMAC-signed cookie>  — the "browser path". Server
         sets this cookie when serving / so the SPA never sees the raw token.

    If NCGA_AUTH_TOKEN is unset, auth is disabled (backwards-compatible).
    """
    expected = os.environ.get("NCGA_AUTH_TOKEN", "").strip()
    if not expected:
        return True

    # Track 1 — bearer token. Malformed/wrong header should not block a valid
    # cookie, so fall through on mismatch.
    auth = environ.get("HTTP_AUTHORIZATION", "").strip()
    if auth.lower().startswith("bearer ") and _const_eq(auth[7:].strip(), expected):
        return True

    # Track 2 — signed session cookie (SPA).
    cookie_val = _get_cookie(environ, _SESSION_COOKIE_NAME)
    return bool(cookie_val and _verify_session_cookie(cookie_val))


# Cycle 20 — session cookie helpers.
#
# Why HMAC-signed cookie instead of "session ID + server-side store":
#   We have no DB. The state we need (was-issued-by-us, not-too-old) fits in
#   a single signed cookie value: <timestamp>.<nonce>.<sig>. Verification is
#   stateless and O(1). Forgery requires NCGA_AUTH_TOKEN.
#
# Why NCGA_AUTH_TOKEN is the signing key:
#   It's already the gatekeeper for the API. Reusing it means rotating the
#   token instantly invalidates all outstanding cookies (defense in depth).
_SESSION_COOKIE_NAME = "ncga_sess"
_SESSION_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days

# Cycle 21 self-audit #6: stateless cookie has no revocation without this.
# When a user logs out, we add their cookie's (timestamp, nonce) pair to an
# in-memory set so subsequent requests carrying it 401. Auto-pruned when
# entries pass the 30-day expiry. A process restart clears it — acceptable
# since all in-flight cookies are also validated against NCGA_AUTH_TOKEN.
_REVOKED_COOKIES: set[tuple[str, str]] = set()
_REVOKED_LOCK = threading.Lock()


def _revoke_cookie(value: str) -> bool:
    """Mark a cookie value as revoked. Returns True if added (incl. duplicate)."""
    parts = value.split(".")
    if len(parts) != 3:
        return False
    ts_str, nonce, _sig = parts
    with _REVOKED_LOCK:
        _REVOKED_COOKIES.add((ts_str, nonce))
        cutoff = time.time() - _SESSION_COOKIE_MAX_AGE
        expired = {(t, n) for (t, n) in _REVOKED_COOKIES if t.isdigit() and int(t) < cutoff}
        _REVOKED_COOKIES.difference_update(expired)
    return True


def _is_cookie_revoked(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 3:
        return False
    ts_str, nonce, _sig = parts
    with _REVOKED_LOCK:
        return (ts_str, nonce) in _REVOKED_COOKIES


def _make_session_cookie() -> str | None:
    """Mint a fresh session cookie value. Returns None if auth is disabled."""
    secret = os.environ.get("NCGA_AUTH_TOKEN", "").strip()
    if not secret:
        return None
    import hashlib
    import hmac as _hmac
    import secrets as _secrets

    ts = str(int(time.time()))
    nonce = _secrets.token_hex(8)
    msg = f"{ts}.{nonce}".encode()
    sig = _hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:24]
    return f"{ts}.{nonce}.{sig}"


def _verify_session_cookie(value: str) -> bool:
    """True iff `value` is a well-formed cookie that we minted and isn't expired."""
    secret = os.environ.get("NCGA_AUTH_TOKEN", "").strip()
    if not secret or not value:
        return False
    import hashlib
    import hmac as _hmac

    parts = value.split(".")
    if len(parts) != 3:
        return False
    ts_str, nonce, sig = parts
    if not ts_str.isdigit() or not sig:
        return False
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    if time.time() - ts > _SESSION_COOKIE_MAX_AGE:
        return False
    expected = _hmac.new(secret.encode(), f"{ts_str}.{nonce}".encode(), hashlib.sha256).hexdigest()[:24]
    if not _hmac.compare_digest(sig, expected):
        return False
    # Cycle 21 self-audit #6: signature valid + not expired, but if user
    # logged out, the (ts, nonce) pair is in the revocation set.
    return not _is_cookie_revoked(value)


def _get_cookie(environ: dict, name: str) -> str | None:
    raw = environ.get("HTTP_COOKIE") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


def _format_session_cookie(value: str, *, secure: bool) -> str:
    """Build a Set-Cookie header value with the right flags."""
    parts = [
        f"{_SESSION_COOKIE_NAME}={value}",
        f"Max-Age={_SESSION_COOKIE_MAX_AGE}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


# Static (header-only) security headers. CSP is built per-request in _security_headers()
# because it carries a per-response nonce.
_STATIC_SECURITY_HEADERS: list[tuple[str, str]] = [
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "geolocation=(), microphone=(), camera=()"),
    ("X-Frame-Options", "DENY"),
    # HSTS — browsers auto-upgrade to HTTPS for a year; only sent when behind a TLS proxy.
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
]


def _build_csp(nonce: str | None = None) -> str:
    """Cycle 13: script-src has no 'unsafe-inline'; boot-screen FOUC script gets a nonce.
    Cycle 20: style-src ALSO drops 'unsafe-inline' now. All previously-inline
    `style="..."` sites in app.js have been refactored to DOM-API style sets
    (which CSP doesn't gate) or fixed CSS rules. External stylesheets remain
    allowed via the explicit https://* allowlist.
    """
    script_src = "'self'"
    if nonce:
        script_src += f" 'nonce-{nonce}'"
    return (
        "default-src 'self'; "
        "img-src 'self' data: "
        "https://upload.wikimedia.org https://commons.wikimedia.org "
        "https://images.unsplash.com; "
        "style-src 'self' https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
        f"script-src {script_src}; "
        "font-src 'self' https://cdnjs.cloudflare.com https://fonts.gstatic.com; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


SECURITY_HEADERS = _STATIC_SECURITY_HEADERS  # backwards-compat name


def _security_headers(extra: list[tuple[str, str]], *, csp_nonce: str | None = None) -> list[tuple[str, str]]:
    return [
        ("Content-Security-Policy", _build_csp(csp_nonce)),
        *_STATIC_SECURITY_HEADERS,
        *extra,
    ]


def json_response(start_response: Callable, status: str, payload: dict) -> list[bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response(
        status,
        _security_headers(
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ]
        ),
    )
    return [body]


def static_response(start_response: Callable, path: Path, *, environ: dict | None = None) -> list[bytes]:
    import base64 as _b64
    import secrets as _secrets

    content = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    is_text = content_type.startswith("text/") or content_type in (
        "application/javascript",
        "application/json",
    )
    csp_nonce = None
    if path.name == "index.html":
        # Cycle 13: per-response CSP nonce. Inline boot script in index.html must
        # carry nonce="..." or the browser blocks it.
        csp_nonce = _b64.urlsafe_b64encode(_secrets.token_bytes(12)).rstrip(b"=").decode("ascii")
        content = content.replace(
            b"<script>",
            f'<script nonce="{csp_nonce}">'.encode(),
            1,  # only the first inline boot script; external <script src=...> are unaffected
        )
        # Cycle 20: stop injecting the raw NCGA_AUTH_TOKEN into the served HTML.
        # The SPA now relies on an HMAC-signed session cookie set below. We
        # inject a no-secret marker so app.js can detect that auth-is-on and
        # avoid its own legacy fallback. Cookie-mode is silently activated.
        if os.environ.get("NCGA_AUTH_TOKEN", "").strip():
            meta = b'<meta name="ncga-auth-mode" content="cookie">'
            content = content.replace(b"<head>", b"<head>\n    " + meta, 1)
    headers = [
        ("Content-Type", f"{content_type}; charset=utf-8" if is_text else content_type),
        ("Content-Length", str(len(content))),
        ("Cache-Control", "public, max-age=300" if path.name != "index.html" else "no-store"),
    ]
    # Cycle 20: mint + set the SPA session cookie on every index.html serve.
    # Cookie is HMAC-signed against NCGA_AUTH_TOKEN so forgery without the
    # secret is infeasible. Same-site Lax allows top-level navigation from
    # external links while blocking cross-site POSTs (CSRF-resistant).
    if path.name == "index.html":
        cookie_val = _make_session_cookie()
        if cookie_val:
            secure_flag = (bool(environ) and environ.get("wsgi.url_scheme") == "https") or (
                environ and (environ.get("HTTP_X_FORWARDED_PROTO") or "").lower() == "https"
            )
            headers.append(("Set-Cookie", _format_session_cookie(cookie_val, secure=bool(secure_flag))))
    start_response("200 OK", _security_headers(headers, csp_nonce=csp_nonce))
    return [content]


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _safe_static_path(rel: str) -> Path | None:
    """Return resolved path inside STATIC_DIR, or None if traversal attempted."""
    if not rel:
        return None
    candidate = (STATIC_DIR / rel).resolve()
    try:
        candidate.relative_to(STATIC_DIR)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


# ---------------- rate limiter ----------------


class RateLimiter:
    """Sliding-window per-IP limiter. Thread-safe; no external deps."""

    def __init__(self, *, per_minute: int) -> None:
        self.per_minute = per_minute
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_gc = time.monotonic()

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.per_minute:
                return False
            bucket.append(now)
            # Periodic cleanup so bucket dict doesn't grow unbounded.
            if now - self._last_gc > 300:
                self._gc(cutoff)
                self._last_gc = now
        return True

    def _gc(self, cutoff: float) -> None:
        empties = [k for k, v in self._buckets.items() if not v or v[-1] < cutoff]
        for k in empties:
            self._buckets.pop(k, None)


class DailyCounter:
    """Cycle 18 v2: per-IP per-calendar-day cap, shared across all LLM endpoints.

    Why: the existing per-minute RateLimiter caps burst rate but not aggregate
    daily volume. A single bearer-token holder, staying within 30/min, can fire
    43,200 LLM calls per day — meaningful API bill. This class adds a daily
    ceiling. In-memory; resets at local midnight (key = ISO date string).

    Not persistent: a server restart wipes the counter. That's acceptable —
    restart is rare, and the per-minute limiter still bounds abuse during the
    fresh start. If you want persistence, swap the dict for a JSON-on-disk
    structure under ~/.local/share/ncga/.
    """

    def __init__(self, *, per_day: int) -> None:
        self.per_day = per_day
        # key = (ip, iso_date); value = count
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self._last_gc_day: str = ""

    def _today(self) -> str:
        # Local timezone midnight rollover — matches user's calendar expectation.
        # If you ever go multi-region, switch to UTC and document.
        from datetime import date as _date

        return _date.today().isoformat()

    def allow(self, ip: str, *, units: int = 1) -> bool:
        """Atomically check + consume `units` units for `ip` today.
        Returns True iff post-consumption count would still be ≤ per_day.
        `units > 1` exists for batch endpoints whose single HTTP request fans
        out into many LLM calls — they should account honestly."""
        if self.per_day <= 0:
            return True
        if units <= 0:
            return True
        today = self._today()
        with self._lock:
            if today != self._last_gc_day:
                self._counts = {k: v for k, v in self._counts.items() if k[1] == today}
                self._last_gc_day = today
            cur = self._counts.get((ip, today), 0)
            if cur + units > self.per_day:
                return False
            self._counts[(ip, today)] = cur + units
        return True

    def remaining(self, ip: str) -> int:
        with self._lock:
            return max(0, self.per_day - self._counts.get((ip, self._today()), 0))


class FeedbackStore:
    """Cycle 20: append-only JSONL for user reflection-form submissions.

    Plain JSONL (one JSON object per line) so the operator can `tail -f`,
    grep, or pipe through `jq`. No DB, no migrations. Each write is one
    open-append-close under a lock so concurrent waitress threads don't
    interleave half-written lines.

    Lives at NCGA_FEEDBACK_STORE if set, else ~/.local/share/ncga/feedback.jsonl
    (fallback to BASE_DIR/.ncga-feedback.jsonl for read-only home dirs).
    Records contain salted SHA-256(ip) — never the raw IP — so the file is
    safe to ship to collaborators.

    Self-audit #1 (Cycle 21): file is created with mode 0o600 so a shared
    machine doesn't expose users' emails to other local users.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    @classmethod
    def default_path(cls) -> Path:
        override = os.environ.get("NCGA_FEEDBACK_STORE", "").strip()
        if override:
            return Path(override)
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        user_dir = Path(xdg) / "ncga" if xdg else Path.home() / ".local" / "share" / "ncga"
        try:
            user_dir.mkdir(parents=True, exist_ok=True)
            return user_dir / "feedback.jsonl"
        except OSError:
            return BASE_DIR / ".ncga-feedback.jsonl"

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            need_chmod = not self.path.exists()
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if need_chmod:
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    logger.warning("could not chmod 0o600 on %s", self.path)


def _hash_ip(ip: str) -> str:
    """Salted SHA-256 prefix so per-IP grouping is possible without storing the raw IP.
    Salt is derived from NCGA_AUTH_TOKEN when set (rotates with the deploy) so the same
    user appears stable within one deployment but not linkable across deployments."""
    import hashlib

    salt = os.environ.get("NCGA_AUTH_TOKEN", "ncga-feedback-default-salt")
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:12]


def _trust_forwarded_for() -> bool:
    """Cycle 13: only trust X-Forwarded-For when explicitly opted in.
    Without this gate, an attacker can rotate the header to bypass per-IP rate limiting."""
    val = os.environ.get("NCGA_TRUST_FORWARDED_FOR", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def client_ip(environ: dict) -> str:
    # Only trust X-Forwarded-For if explicitly enabled (i.e., behind a trusted reverse proxy).
    # Otherwise an attacker can spoof per-IP rate limit by rotating the header.
    if _trust_forwarded_for():
        fwd = environ.get("HTTP_X_FORWARDED_FOR", "").strip()
        if fwd:
            return fwd.split(",")[0].strip()
    return environ.get("REMOTE_ADDR", "unknown")


# ---------------- application ----------------


class App:
    def __init__(
        self,
        *,
        rewrite_service: RewriteService | None = None,
        rate_limit_per_min: int | None = None,
        batch_rate_limit_per_min: int | None = None,
        max_body_bytes: int | None = None,
        quality_store: QualityStore | None = None,
    ) -> None:
        # If a quality_store is given, attach it to the service so prompt overrides apply.
        if quality_store is None:
            store_path = os.environ.get("NCGA_QUALITY_STORE", "").strip()
            if store_path:
                quality_store = QualityStore(path=Path(store_path))
            else:
                # Cycle 13: prefer user data dir over source dir.
                # Falls back to source dir if user dir is unwritable (e.g., read-only deployment).
                xdg = os.environ.get("XDG_DATA_HOME", "").strip()
                user_dir = Path(xdg) / "ncga" if xdg else Path.home() / ".local" / "share" / "ncga"
                try:
                    user_dir.mkdir(parents=True, exist_ok=True)
                    quality_store_path = user_dir / "quality.json"
                except OSError:
                    quality_store_path = BASE_DIR / ".ncga-quality.json"
                # If a legacy file exists in source dir, prefer it (one-time migration on next save)
                legacy = BASE_DIR / ".ncga-quality.json"
                if legacy.is_file() and not quality_store_path.is_file():
                    quality_store_path = legacy
                quality_store = QualityStore(path=quality_store_path)
        self.quality_store = quality_store
        if rewrite_service is None:
            rewrite_service = RewriteService(quality_store=quality_store)
        elif rewrite_service.quality_store is None:
            rewrite_service.quality_store = quality_store
        self.rewrite_service = rewrite_service
        rl = (
            rate_limit_per_min
            if rate_limit_per_min is not None
            else int(os.environ.get("NCGA_RATE_LIMIT_PER_MIN", DEFAULT_RATE_LIMIT_PER_MIN))
        )
        self.rate_limiter = RateLimiter(per_minute=rl)
        # Separate (smaller) bucket for batch — each batch is 1 click but many sub-LLM calls.
        brl = (
            batch_rate_limit_per_min
            if batch_rate_limit_per_min is not None
            else int(os.environ.get("NCGA_BATCH_RATE_LIMIT_PER_MIN", DEFAULT_BATCH_RATE_LIMIT_PER_MIN))
        )
        self.batch_limiter = RateLimiter(per_minute=brl)
        # Cycle 18 (Function 1): dedicated tight bucket for /api/characterize.
        # Each call costs one LLM round; users opt-in by clicking the wizard. A
        # leaky/abusive client could otherwise drain the API key — keep the cap
        # below the interactive rewrite limit.
        crl = int(
            os.environ.get(
                "NCGA_CHARACTERIZE_RATE_LIMIT_PER_MIN",
                DEFAULT_CHARACTERIZE_RATE_LIMIT_PER_MIN,
            )
        )
        self.characterize_limiter = RateLimiter(per_minute=crl)
        # Cycle 18 v2: shared daily ceiling across all LLM-spending endpoints.
        daily_cap = int(os.environ.get("NCGA_DAILY_LLM_CAP_PER_IP", DEFAULT_DAILY_LLM_CAP_PER_IP))
        self.daily_counter = DailyCounter(per_day=daily_cap)
        self.max_body_bytes = (
            max_body_bytes
            if max_body_bytes is not None
            else int(os.environ.get("NCGA_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES))
        )
        # Smaller body cap on /api/characterize — recipient + mood are short.
        self.characterize_max_body_bytes = int(
            os.environ.get(
                "NCGA_CHARACTERIZE_MAX_BODY_BYTES",
                DEFAULT_CHARACTERIZE_MAX_BODY_BYTES,
            )
        )
        # Cycle 20: feedback form. Tight rate limit (no LLM call, but spam guard).
        # Storage is plain JSONL the operator can tail / aggregate.
        self.feedback_limiter = RateLimiter(
            per_minute=int(
                os.environ.get("NCGA_FEEDBACK_RATE_LIMIT_PER_MIN", DEFAULT_FEEDBACK_RATE_LIMIT_PER_MIN)
            )
        )
        self.feedback_max_body_bytes = int(
            os.environ.get("NCGA_FEEDBACK_MAX_BODY_BYTES", DEFAULT_FEEDBACK_MAX_BODY_BYTES)
        )
        self.feedback_store = FeedbackStore(FeedbackStore.default_path())

    def _check_daily_cap(self, ip: str, start_response: Callable, endpoint: str, *, units: int = 1):
        """Cycle 18 v2: shared per-IP daily ceiling check. Call this in every
        LLM-spending handler BEFORE the per-minute limiter. `units` is the
        expected number of LLM calls this request will generate — most endpoints
        pass 1; batch passes items*varieties. Returns the 429 response on cap,
        else None."""
        if not self.daily_counter.allow(ip, units=units):
            logger.info("daily_cap_exceeded ip=%s endpoint=%s units=%d", ip, endpoint, units)
            return json_response(
                start_response,
                "429 Too Many Requests",
                {
                    "error": (
                        f"今日 LLM 调用上限已到（每个 IP 每日 {self.daily_counter.per_day} 次）。明天再试。"
                    )
                },
            )
        return None

    def _route_table(self) -> dict[tuple[str, str], Callable]:
        """Cycle 18 architecture cleanup: replace the 16-line if-chain dispatch with
        a (method, path) → handler table. Lookup is O(1) and adding a new endpoint
        is one tuple insertion. GET handlers are wrapped in lambdas because they
        are simple closures over instance state (no separate `handle_*` method
        was worth defining for them)."""
        return {
            ("GET", "/"): lambda env, sr: static_response(sr, STATIC_DIR / "index.html", environ=env),
            ("GET", "/api/presets"): lambda env, sr: json_response(
                sr, "200 OK", {"presets": preset_options()}
            ),
            ("GET", "/api/scenarios"): lambda env, sr: json_response(
                sr, "200 OK", {"scenarios": scenario_options()}
            ),
            ("GET", "/api/healthz"): lambda env, sr: json_response(sr, "200 OK", {"status": "ok"}),
            ("GET", "/api/quality-stats"): self.handle_quality_stats,
            ("GET", "/api/phrase-of-the-day"): self.handle_phrase_of_the_day,
            ("POST", "/api/rewrite"): self.handle_rewrite,
            ("POST", "/api/rewrite-stream"): self.handle_rewrite_stream,
            ("POST", "/api/rewrite-batch"): self.handle_rewrite_batch,
            ("POST", "/api/rate"): self.handle_rate,
            ("POST", "/api/explain"): self.handle_explain,
            ("POST", "/api/characterize"): self.handle_characterize,
            ("POST", "/api/meta-refine"): self.handle_meta_refine,
            ("POST", "/api/quality-stats/clear-override"): self.handle_clear_override,
            ("POST", "/api/override-activate"): self.handle_override_activate,
            ("POST", "/api/override-reject"): self.handle_override_reject,
            ("POST", "/api/feedback"): self.handle_feedback,
            ("POST", "/api/logout"): self.handle_logout,
        }

    def __call__(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "/")

        # Cycle 13: bearer-token gate (when NCGA_AUTH_TOKEN is set in env).
        # Applied to POST /api/* — GET stays open so the SPA can boot.
        # Cycle 21 #6: /api/logout is exempt — calling logout when already
        # logged-out / stale-cookie should always be a 200 no-op so the SPA
        # can clean up without first proving auth.
        _AUTH_EXEMPT_POST_PATHS = {"/api/logout"}
        if (
            method == "POST"
            and path.startswith("/api/")
            and path not in _AUTH_EXEMPT_POST_PATHS
            and not _check_auth(environ)
        ):
            return json_response(
                start_response,
                "401 Unauthorized",
                {"error": "Authentication required (Bearer token)."},
            )

        # Static files (prefix match — distinct from the table's exact lookup)
        if method == "GET" and path.startswith("/static/"):
            file_path = _safe_static_path(path.removeprefix("/static/"))
            if file_path is None:
                return json_response(start_response, "404 Not Found", {"error": "Not found."})
            return static_response(start_response, file_path, environ=environ)

        handler = self._route_table().get((method, path))
        if handler is not None:
            return handler(environ, start_response)
        return json_response(start_response, "404 Not Found", {"error": "Not found."})

    def handle_rewrite(self, environ: dict, start_response: Callable) -> list[bytes]:
        ip = client_ip(environ)
        cap_err = self._check_daily_cap(ip, start_response, "rewrite")
        if cap_err is not None:
            return cap_err
        if not self.rate_limiter.allow(ip):
            logger.info("rate_limited ip=%s", ip)
            return json_response(
                start_response,
                "429 Too Many Requests",
                {"error": "Rate limit exceeded. Please slow down."},
            )

        payload, err = self._read_json_body(environ, start_response)
        if err is not None:
            return err
        try:
            text = payload["text"]
            target = parse_variety(payload["target_variety"])
            # Scenario is optional — silently default to FRIENDS_CASUAL if missing/unknown.
            # Cycle 16: the chosen scenario is now echoed in the response as `effective_scenario`
            # so a stale-frontend / typo'd scenario surfaces visibly without breaking the contract.
            scenario = parse_scenario(payload.get("scenario"))
            glossary_lines = _coerce_glossary(payload.get("glossary"))
        except KeyError as exc:
            field = exc.args[0] if exc.args else "<unknown>"
            return json_response(
                start_response, "400 Bad Request", {"error": f"Missing required field: {field!r}."}
            )
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})

        if not isinstance(text, str):
            return json_response(start_response, "400 Bad Request", {"error": "`text` must be a string."})
        if len(text) > MAX_INPUT_CHARS * 4:  # before normalize, hard cap on raw payload size
            return json_response(start_response, "400 Bad Request", {"error": "Text too long."})

        try:
            result = self.rewrite_service.rewrite(
                text, target, scenario=scenario, glossary_lines=glossary_lines
            )
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})

        data = result.as_dict()
        data["effective_scenario"] = scenario.value
        return json_response(start_response, "200 OK", data)

    def handle_explain(self, environ: dict, start_response: Callable) -> list[bytes]:
        """Take {original, rewritten, target_variety} and return {summary, points[]}."""
        ip = client_ip(environ)
        cap_err = self._check_daily_cap(ip, start_response, "explain")
        if cap_err is not None:
            return cap_err
        # Reuse the same rate limiter — explain is also LLM-backed and costs money.
        if not self.rate_limiter.allow(ip):
            logger.info("rate_limited ip=%s endpoint=explain", ip)
            return json_response(
                start_response,
                "429 Too Many Requests",
                {"error": "Rate limit exceeded. Please slow down."},
            )

        payload, err = self._read_json_body(environ, start_response)
        if err is not None:
            return err
        try:
            original = payload["original"]
            rewritten = payload["rewritten"]
            target = parse_variety(payload["target_variety"])
        except KeyError as exc:
            field = exc.args[0] if exc.args else "<unknown>"
            return json_response(
                start_response, "400 Bad Request", {"error": f"Missing required field: {field!r}."}
            )
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})

        if not isinstance(original, str) or not isinstance(rewritten, str):
            return json_response(
                start_response,
                "400 Bad Request",
                {"error": "`original` and `rewritten` must be strings."},
            )

        try:
            data = self.rewrite_service.explain(original, rewritten, target)
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})
        except RewriteError as exc:
            return json_response(start_response, "503 Service Unavailable", {"error": str(exc)})

        return json_response(start_response, "200 OK", data)

    # ------------------ Cycle 18 (Function 1): 情境向导 ------------------

    def handle_characterize(self, environ: dict, start_response: Callable) -> list[bytes]:
        """Two-question wizard → structured profile.
        Security posture (matches issue #3 + the user's emphasis on cost+abuse risk):
          * Auth gate (already enforced upstream by _check_auth)
          * Dedicated tight rate limiter (default 6/min/IP, separate bucket)
          * 1 KB body cap (recipient + mood are <120 chars each)
          * recipient + mood individually clipped to 120 chars in the service layer
          * One LLM call per request — no fan-out
          * No persistence (no PII storage)
        """
        ip = client_ip(environ)
        cap_err = self._check_daily_cap(ip, start_response, "characterize")
        if cap_err is not None:
            return cap_err
        if not self.characterize_limiter.allow(ip):
            logger.info("rate_limited ip=%s endpoint=characterize", ip)
            return json_response(
                start_response,
                "429 Too Many Requests",
                {"error": "情境向导每分钟最多 6 次。请稍候再试。"},
            )
        payload, err = self._read_json_body(
            environ, start_response, max_bytes=self.characterize_max_body_bytes
        )
        if err is not None:
            return err
        recipient = payload.get("recipient", "")
        mood = payload.get("mood", "")
        if not isinstance(recipient, str) or not isinstance(mood, str):
            return json_response(
                start_response,
                "400 Bad Request",
                {"error": "`recipient` and `mood` must be strings."},
            )
        try:
            data = self.rewrite_service.characterize(recipient, mood)
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})
        except RewriteError as exc:
            return json_response(start_response, "503 Service Unavailable", {"error": str(exc)})
        return json_response(start_response, "200 OK", data)

    # ------------------ Cycle 18 (Function 2): 今日方言一句 ------------------

    def handle_phrase_of_the_day(self, environ: dict, start_response: Callable) -> list[bytes]:
        """Daily wisdom phrase rewritten into all 10 varieties + a landmark image.
        Cached per calendar day in ~/.local/share/ncga/phrase-cache.json — only the
        first call of the day pays the LLM cost (10 rewrites)."""
        from native_chinese_assistant.daily_phrase import _cache_path, get_phrase_of_the_day

        # Cycle 18 v2: only count against the daily LLM cap if we'll actually
        # generate today's payload. If the cache is already warm for today,
        # this is a free read (no LLM call), so skip the counter.
        ip = client_ip(environ)
        try:
            cache_warm = _cache_path().is_file()
        except OSError:
            cache_warm = False
        if not cache_warm:
            # Generation costs ~10 LLM calls (one per variety); count honestly.
            cap_err = self._check_daily_cap(ip, start_response, "phrase-of-the-day", units=10)
            if cap_err is not None:
                return cap_err

        try:
            data = get_phrase_of_the_day(self.rewrite_service)
        except RewriteError as exc:
            return json_response(start_response, "503 Service Unavailable", {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("phrase-of-the-day failed: %s", exc)
            return json_response(
                start_response,
                "503 Service Unavailable",
                {"error": "phrase-of-the-day temporarily unavailable"},
            )
        return json_response(start_response, "200 OK", data)

    # ------------------ streaming + batch (Cycle 7-8) ------------------

    def _read_json_body(self, environ: dict, start_response: Callable, max_bytes: int | None = None):
        """Common: read+parse JSON body. Returns (payload_dict, error_response_or_none).

        Cycle 16: each failure class gets a distinct, actionable message
        (RFC 7807 spirit). Replaces the prior generic 'Invalid JSON payload.'.
        Cycle 18: optional `max_bytes` lets endpoints with tiny payloads (like
        /api/characterize) enforce a tighter cap than the default 64 KB.
        """
        cap = max_bytes if max_bytes is not None else self.max_body_bytes
        try:
            length = int(environ.get("CONTENT_LENGTH") or "0")
        except ValueError:
            length = 0
        if length < 0 or length > cap:
            return None, json_response(
                start_response,
                "413 Payload Too Large",
                {"error": f"Request body exceeds {cap} bytes."},
            )
        raw = environ["wsgi.input"].read(length) if length else b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, json_response(
                start_response, "400 Bad Request", {"error": "Request body must be valid UTF-8."}
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, json_response(
                start_response,
                "400 Bad Request",
                {"error": f"Malformed JSON: {exc.msg} at line {exc.lineno} column {exc.colno}."},
            )
        if not isinstance(payload, dict):
            return None, json_response(
                start_response,
                "400 Bad Request",
                {"error": "Request body must be a JSON object."},
            )
        return payload, None

    def handle_rewrite_stream(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        """SSE endpoint. Streams partial rewrite text as the LLM emits tokens."""
        ip = client_ip(environ)
        cap_err = self._check_daily_cap(ip, start_response, "rewrite-stream")
        if cap_err is not None:
            return cap_err
        if not self.rate_limiter.allow(ip):
            logger.info("rate_limited ip=%s endpoint=rewrite-stream", ip)
            return json_response(
                start_response,
                "429 Too Many Requests",
                {"error": "Rate limit exceeded. Please slow down."},
            )
        payload, err = self._read_json_body(environ, start_response)
        if err is not None:
            return err
        try:
            text = payload["text"]
            target = parse_variety(payload["target_variety"])
            scenario = parse_scenario(payload.get("scenario"))
            glossary_lines = _coerce_glossary(payload.get("glossary"))
        except KeyError as exc:
            field = exc.args[0] if exc.args else "<unknown>"
            return json_response(
                start_response, "400 Bad Request", {"error": f"Missing required field: {field!r}."}
            )
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})
        if not isinstance(text, str):
            return json_response(start_response, "400 Bad Request", {"error": "`text` must be a string."})

        start_response(
            "200 OK",
            _security_headers(
                [
                    ("Content-Type", "text/event-stream; charset=utf-8"),
                    ("Cache-Control", "no-cache, no-transform"),
                    ("X-Accel-Buffering", "no"),  # tell nginx not to buffer if proxied
                ]
            ),
        )
        return _sse_iter_rewrite_stream(
            self.rewrite_service, text, target, scenario, glossary_lines=glossary_lines
        )

    def handle_rate(self, environ: dict, start_response: Callable) -> list[bytes]:
        """Rate the 'native-ness' of a rewritten text: small LLM call, returns {score, reason}."""
        ip = client_ip(environ)
        cap_err = self._check_daily_cap(ip, start_response, "rate")
        if cap_err is not None:
            return cap_err
        if not self.rate_limiter.allow(ip):
            logger.info("rate_limited ip=%s endpoint=rate", ip)
            return json_response(
                start_response,
                "429 Too Many Requests",
                {"error": "Rate limit exceeded. Please slow down."},
            )
        payload, err = self._read_json_body(environ, start_response)
        if err is not None:
            return err
        try:
            rewritten = payload["rewritten"]
            target = parse_variety(payload["target_variety"])
            scenario = parse_scenario(payload.get("scenario"))
            original = str(payload.get("original", "") or "")
        except KeyError as exc:
            field = exc.args[0] if exc.args else "<unknown>"
            return json_response(
                start_response, "400 Bad Request", {"error": f"Missing required field: {field!r}."}
            )
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})
        if not isinstance(rewritten, str):
            return json_response(
                start_response, "400 Bad Request", {"error": "`rewritten` must be a string."}
            )
        try:
            data = self.rewrite_service.rate_quality(
                rewritten,
                target,
                scenario=scenario,
                original=original,
            )
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})
        except RewriteError as exc:
            return json_response(start_response, "503 Service Unavailable", {"error": str(exc)})
        # Surface whether reflection is now warranted, so the UI can prompt user
        data = {**data, "needs_reflection": self.quality_store.needs_reflection(target.value, scenario.value)}
        return json_response(start_response, "200 OK", data)

    def handle_rewrite_batch(self, environ: dict, start_response: Callable) -> Iterable[bytes]:
        """SSE endpoint streaming batch progress. Body:
        {items: [str, ...], target_varieties: [variety_value, ...], scenario?: str, max_parallel?: int (1-3)}
        """
        ip = client_ip(environ)
        # Higher rate-limit bucket for batch requests (they cost a lot per click but rare).
        if not self.batch_limiter.allow(ip):
            logger.info("rate_limited ip=%s endpoint=rewrite-batch", ip)
            return json_response(
                start_response,
                "429 Too Many Requests",
                {"error": "Batch rate limit exceeded. Please wait."},
            )
        payload, err = self._read_json_body(environ, start_response)
        if err is not None:
            return err
        try:
            items = payload["items"]
            target_varieties = payload["target_varieties"]
            scenario = parse_scenario(payload.get("scenario"))
            glossary_lines = _coerce_glossary(payload.get("glossary"))
            max_parallel = int(payload.get("max_parallel") or 3)
        except KeyError as exc:
            field = exc.args[0] if exc.args else "<unknown>"
            return json_response(
                start_response, "400 Bad Request", {"error": f"Missing required field: {field!r}."}
            )
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})
        if not isinstance(items, list) or not items:
            return json_response(
                start_response, "400 Bad Request", {"error": "`items` must be a non-empty list."}
            )
        if not isinstance(target_varieties, list) or not target_varieties:
            return json_response(
                start_response,
                "400 Bad Request",
                {"error": "`target_varieties` must be a non-empty list."},
            )
        # Hard cap: at most 100 items × at most 4 varieties to bound cost.
        if len(items) > 100:
            return json_response(start_response, "400 Bad Request", {"error": "Too many items (max 100)."})
        if len(target_varieties) > 4:
            return json_response(start_response, "400 Bad Request", {"error": "Too many varieties (max 4)."})
        try:
            varieties = [parse_variety(v) for v in target_varieties]
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})
        # Cycle 18 v2: daily-cap accounting AFTER we know the cell count. Each cell is
        # one LLM call; charge the counter the full fan-out so batch can't bypass the cap.
        cap_err = self._check_daily_cap(
            ip, start_response, "rewrite-batch", units=len(items) * len(varieties)
        )
        if cap_err is not None:
            return cap_err
        # Validate all items are strings, non-empty, within length cap
        for i, item in enumerate(items):
            if not isinstance(item, str) or not item.strip():
                return json_response(
                    start_response, "400 Bad Request", {"error": f"Item #{i} is empty or non-string."}
                )
            if len(item) > MAX_INPUT_CHARS:
                return json_response(
                    start_response,
                    "400 Bad Request",
                    {"error": f"Item #{i} exceeds {MAX_INPUT_CHARS} characters."},
                )

        start_response(
            "200 OK",
            _security_headers(
                [
                    ("Content-Type", "text/event-stream; charset=utf-8"),
                    ("Cache-Control", "no-cache, no-transform"),
                    ("X-Accel-Buffering", "no"),
                ]
            ),
        )
        return _sse_iter_batch(
            self.rewrite_service,
            items,
            varieties,
            scenario,
            glossary_lines=glossary_lines,
            max_parallel=max_parallel,
        )

    def handle_meta_refine(self, environ: dict, start_response: Callable) -> list[bytes]:
        """Trigger Reflexion-style prompt refinement for (variety, scenario)."""
        ip = client_ip(environ)
        cap_err = self._check_daily_cap(ip, start_response, "meta-refine")
        if cap_err is not None:
            return cap_err
        if not self.rate_limiter.allow(ip):
            return json_response(
                start_response,
                "429 Too Many Requests",
                {"error": "Rate limit exceeded. Please slow down."},
            )
        payload, err = self._read_json_body(environ, start_response)
        if err is not None:
            return err
        try:
            target = parse_variety(payload["target_variety"])
            scenario = parse_scenario(payload.get("scenario"))
        except KeyError as exc:
            field = exc.args[0] if exc.args else "<unknown>"
            return json_response(
                start_response, "400 Bad Request", {"error": f"Missing required field: {field!r}."}
            )
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})
        try:
            data = self.rewrite_service.meta_refine(target, scenario)
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})
        except RewriteError as exc:
            return json_response(start_response, "503 Service Unavailable", {"error": str(exc)})
        return json_response(start_response, "200 OK", data)

    def handle_override_activate(self, environ: dict, start_response: Callable) -> list[bytes]:
        """Promote a draft override to active. Optionally accept user-edited addendum text."""
        payload, err = self._read_json_body(environ, start_response)
        if err is not None:
            return err
        try:
            target = parse_variety(payload["target_variety"])
            scenario = parse_scenario(payload.get("scenario"))
        except KeyError as exc:
            field = exc.args[0] if exc.args else "<unknown>"
            return json_response(
                start_response, "400 Bad Request", {"error": f"Missing required field: {field!r}."}
            )
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})
        # Optional user override of the addendum text (lets them edit before activating)
        edited_addendum = payload.get("addendum")
        if edited_addendum is not None and not isinstance(edited_addendum, str):
            return json_response(start_response, "400 Bad Request", {"error": "`addendum` must be a string."})
        if edited_addendum is not None and len(edited_addendum) > 2000:
            return json_response(
                start_response, "400 Bad Request", {"error": "Addendum too long (max 2000 chars)."}
            )
        ok = self.quality_store.activate_override(
            target.value,
            scenario.value,
            addendum=edited_addendum,
            reason=payload.get("reason"),
        )
        return json_response(start_response, "200 OK", {"activated": ok})

    def handle_override_reject(self, environ: dict, start_response: Callable) -> list[bytes]:
        """Discard a draft without activating. Stats are kept; cooldown still applies."""
        payload, err = self._read_json_body(environ, start_response)
        if err is not None:
            return err
        try:
            target = parse_variety(payload["target_variety"])
            scenario = parse_scenario(payload.get("scenario"))
        except KeyError as exc:
            field = exc.args[0] if exc.args else "<unknown>"
            return json_response(
                start_response, "400 Bad Request", {"error": f"Missing required field: {field!r}."}
            )
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})
        rejected = self.quality_store.reject_draft(target.value, scenario.value)
        return json_response(start_response, "200 OK", {"rejected": rejected})

    def handle_clear_override(self, environ: dict, start_response: Callable) -> list[bytes]:
        """Reset a (variety, scenario) override back to the default scenario addendum."""
        payload, err = self._read_json_body(environ, start_response)
        if err is not None:
            return err
        try:
            target = parse_variety(payload["target_variety"])
            scenario = parse_scenario(payload.get("scenario"))
        except KeyError as exc:
            field = exc.args[0] if exc.args else "<unknown>"
            return json_response(
                start_response, "400 Bad Request", {"error": f"Missing required field: {field!r}."}
            )
        except ValueError as exc:
            return json_response(start_response, "400 Bad Request", {"error": str(exc)})
        cleared = self.quality_store.clear_override(target.value, scenario.value)
        return json_response(start_response, "200 OK", {"cleared": cleared})

    def handle_quality_stats(self, environ: dict, start_response: Callable) -> list[bytes]:
        """Cycle 21 self-audit #2: was a bare lambda, no rate limit. A leaked
        cookie / bearer could pull the full bucket dump (all ratings + reasons)
        as fast as the network allowed. Now subject to the standard per-IP
        per-minute limiter, same as /api/rewrite."""
        ip = client_ip(environ)
        if not self.rate_limiter.allow(ip):
            return json_response(
                start_response,
                "429 Too Many Requests",
                {"error": "Rate limit exceeded. Please slow down."},
            )
        return json_response(start_response, "200 OK", {"buckets": self.quality_store.stats_snapshot()})

    def handle_feedback(self, environ: dict, start_response: Callable) -> list[bytes]:
        """Cycle 20: accept a reflection-form submission and append to JSONL.

        No LLM call; rate-limited by a dedicated bucket. Body schema:
          {
            "rating":  1-5 (required, int),
            "liked":   [str, ...] optional,
            "wishlist": [str, ...] optional,
            "note":    free text, ≤800 chars (optional),
            "variety": str (optional context — what they last used),
            "scenario": str (optional context),
            "input_language": str (optional, user-picked or auto-detected),
            "contact": str ≤120 chars (optional)
          }
        Stored record adds: id, ts (ISO UTC), ip_hash, ua (truncated).
        Strips C0 control chars from all persisted text — defends both
        terminal-tail operators and shared `cat`/`grep` viewers.
        """
        ip = client_ip(environ)
        if not self.feedback_limiter.allow(ip):
            return json_response(
                start_response,
                "429 Too Many Requests",
                {"error": "反馈提交太频繁,请等一分钟再试。"},
            )
        payload, err = self._read_json_body(environ, start_response, max_bytes=self.feedback_max_body_bytes)
        if err is not None:
            return err

        # rating: required int 1..5
        try:
            rating_raw = payload["rating"]
        except KeyError:
            return json_response(
                start_response, "400 Bad Request", {"error": "Missing required field: 'rating'."}
            )
        try:
            rating = int(rating_raw)
        except (TypeError, ValueError):
            return json_response(
                start_response, "400 Bad Request", {"error": "`rating` must be an integer 1–5."}
            )
        if not 1 <= rating <= 5:
            return json_response(
                start_response, "400 Bad Request", {"error": "`rating` must be between 1 and 5."}
            )

        def _chips(raw):
            if raw is None:
                return []
            if not isinstance(raw, list):
                return None  # signal validation error to caller
            out: list[str] = []
            for item in raw[:FEEDBACK_CHIP_MAX]:
                s = _CTRL_RE.sub("", str(item)).strip()[:FEEDBACK_CHIP_LEN]
                if s:
                    out.append(s)
            return out

        liked = _chips(payload.get("liked"))
        wishlist = _chips(payload.get("wishlist"))
        if liked is None or wishlist is None:
            return json_response(
                start_response,
                "400 Bad Request",
                {"error": "`liked` and `wishlist` must be arrays of strings."},
            )

        def _trim(field: str, cap: int) -> str:
            v = payload.get(field)
            if v is None:
                return ""
            return _CTRL_RE.sub("", str(v)).strip()[:cap]

        note = _trim("note", FEEDBACK_NOTE_MAX_CHARS)
        contact = _trim("contact", FEEDBACK_CONTACT_MAX_CHARS)
        variety = _trim("variety", 64)
        scenario = _trim("scenario", 64)
        input_language = _trim("input_language", 32)

        from datetime import datetime, timezone
        from secrets import token_hex

        record = {
            "id": f"fb_{int(time.time() * 1000)}_{token_hex(4)}",
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ip_hash": _hash_ip(ip),
            "rating": rating,
            "liked": liked,
            "wishlist": wishlist,
            "note": note,
            "contact": contact,
            "variety": variety,
            "scenario": scenario,
            "input_language": input_language,
            "ua": _CTRL_RE.sub("", str(environ.get("HTTP_USER_AGENT", "")))[:200],
        }
        try:
            self.feedback_store.append(record)
        except OSError as exc:
            logger.exception("feedback store write failed: %s", exc)
            return json_response(
                start_response, "500 Internal Server Error", {"error": "Could not save feedback."}
            )
        logger.info("feedback_received id=%s rating=%d ip_hash=%s", record["id"], rating, record["ip_hash"])
        return json_response(start_response, "200 OK", {"ok": True, "id": record["id"]})

    def handle_logout(self, environ: dict, start_response: Callable) -> list[bytes]:
        """Cycle 21 self-audit #6: SPA-side logout.

        The HMAC cookie is stateless and lives 30 days. Without revocation,
        the only way to kick a session out was to rotate NCGA_AUTH_TOKEN
        (which kicks EVERYONE). Now:
          1. Add the current cookie's (timestamp, nonce) to _REVOKED_COOKIES
             so subsequent requests with it fail _verify_session_cookie.
          2. Send Set-Cookie with Max-Age=0 so the browser drops the value.

        Idempotent. Always returns 200 — calling logout without a cookie is
        a no-op (the user is already "logged out" in any meaningful sense).
        No auth requirement: anyone who CAN reach us with a forged cookie
        only gets that forged cookie revoked, not anyone else's.
        """
        cookie_val = _get_cookie(environ, _SESSION_COOKIE_NAME)
        revoked = False
        if cookie_val:
            revoked = _revoke_cookie(cookie_val)
        # Build the Set-Cookie that immediately expires the browser's copy.
        expire_cookie = f"{_SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
        secure_flag = environ.get("wsgi.url_scheme") == "https" or (
            environ.get("HTTP_X_FORWARDED_PROTO", "").lower() == "https"
        )
        if secure_flag:
            expire_cookie += "; Secure"
        body = json.dumps({"ok": True, "revoked": revoked}, ensure_ascii=False).encode("utf-8")
        start_response(
            "200 OK",
            _security_headers(
                [
                    ("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store"),
                    ("Set-Cookie", expire_cookie),
                ]
            ),
        )
        return [body]


def _coerce_glossary(raw) -> list[str] | None:
    """Coerce the `glossary` field from request body into a clean list of lines.
    Caps at 30 entries and 80 chars each to keep prompt size sane."""
    if not raw:
        return None
    if isinstance(raw, str):
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    elif isinstance(raw, list):
        lines = [str(x).strip() for x in raw if str(x).strip()]
    else:
        return None
    return [ln[:80] for ln in lines[:30]] or None


def _sse_event(event_type: str, data: dict | str) -> bytes:
    """Serialize one SSE event with proper framing (event:, data:, blank line)."""
    if isinstance(data, dict):
        data = json.dumps(data, ensure_ascii=False)
    # Per WHATWG SSE spec: each \n inside data must become its own data: line.
    lines = "\n".join(f"data: {ln}" for ln in data.split("\n"))
    return f"event: {event_type}\n{lines}\n\n".encode()


def _sse_iter_rewrite_stream(service, text, target, scenario, glossary_lines=None):
    """Generator yielding SSE bytes for a single streamed rewrite.

    glossary_lines is currently logged for parity with non-streaming /api/rewrite
    but not threaded into the LLM call (rewrite_stream signature would need expansion).
    """
    try:
        last_partial = ""
        for delta, partial, done in service.rewrite_stream(text, target, scenario=scenario):
            if done:
                yield _sse_event(
                    "done",
                    {
                        "rewritten_text": partial,
                        "target_variety": target.value,
                        "effective_scenario": scenario.value,
                    },
                )
                return
            if partial != last_partial:
                yield _sse_event("chunk", {"partial": partial, "delta": delta})
                last_partial = partial
    except Exception as exc:  # noqa: BLE001
        logger.warning("rewrite-stream error: %s", exc)
        yield _sse_event("error", {"error": str(exc)})


def _sse_iter_batch(service, items, varieties, scenario, glossary_lines=None, max_parallel=3):
    """Generator yielding SSE bytes for batch progress.

    Cycle 9: TRUE PARALLELISM via ThreadPoolExecutor. Within one batch request,
    multiple LLM calls fire concurrently. wsgiref still serves one HTTP request
    at a time, but the LLM calls themselves are I/O-bound so parallel is fine.

    Events:
      meta:  {total, varieties, parallel}
      result: per (idx, variety) — ok or error
      done:   {summary}
    """
    import concurrent.futures

    total = len(items) * len(varieties)
    max_parallel = max(1, min(int(max_parallel or 3), 4))
    yield _sse_event(
        "meta",
        {
            "total": total,
            "items": len(items),
            "varieties": [v.value for v in varieties],
            "parallel": max_parallel,
            "effective_scenario": scenario.value,
        },
    )

    def one(idx, item, variety):
        try:
            result = service.rewrite(item, variety, scenario=scenario, glossary_lines=glossary_lines)
            return (
                idx,
                variety,
                True,
                {
                    "rewritten_text": result.rewritten_text,
                    "degraded": result.degraded,
                    "warning": result.warning,
                    "script": result.script.value,
                },
            )
        except (ValueError, RewriteError) as exc:
            return idx, variety, False, {"error": str(exc)}

    ok_count = 0
    fail_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as ex:
        futures = []
        for idx, item in enumerate(items):
            for variety in varieties:
                futures.append(ex.submit(one, idx, item, variety))
        for fut in concurrent.futures.as_completed(futures):
            idx, variety, ok, payload = fut.result()
            evt = {"idx": idx, "variety": variety.value, "ok": ok, **payload}
            yield _sse_event("result", evt)
            if ok:
                ok_count += 1
            else:
                fail_count += 1
    yield _sse_event("done", {"summary": {"ok": ok_count, "failed": fail_count, "total": total}})


# Module-level WSGI entry point so production servers can use
# `waitress-serve native_chinese_assistant.web:application` directly.
application = App()


# ---------------- logging setup ----------------


def configure_logging(level: str = "INFO") -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                }
            },
            "loggers": {
                "ncga": {"level": level, "handlers": ["console"], "propagate": False},
            },
            "root": {"level": "WARNING", "handlers": ["console"]},
        }
    )


# ---------------- dev server ----------------


def _find_port_holder(port: int) -> str | None:
    """Best-effort: ask `lsof` who's listening on `port`. macOS/Linux only.

    Returns a human-readable string like 'Python (PID 18478)' or None if lookup failed.
    """
    import shutil
    import subprocess

    if shutil.which("lsof") is None:
        return None
    try:
        # -nP: skip DNS / port-name lookup; -i :PORT: filter by listener
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    lines = [ln for ln in out.stdout.splitlines() if ln and not ln.startswith("COMMAND")]
    if not lines:
        return None
    # Format: COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
    parts = lines[0].split()
    if len(parts) < 2:
        return None
    return f"{parts[0]} (PID {parts[1]})"


def _print_port_in_use_help(host: str, port: int, pid_file: Path | None = None) -> None:
    """User-friendly diagnostic when bind fails. Avoid Python tracebacks for ops issues."""
    holder = _find_port_holder(port)
    our_pid = _read_pid_file(pid_file) if pid_file else None
    print()
    print("=" * 72, flush=True)
    print(f"❌  端口 {port} 已被占用，服务无法启动 / Address {host}:{port} already in use.")
    if holder:
        print(f"    占用方 / Held by: {holder}")
    print()
    print("解决方法（任选其一）/ Pick one:")
    if our_pid and _pid_alive(our_pid):
        # The most ergonomic recovery: we left a PID file, just `app.py stop`.
        print("  0) ⚡ 一键收拾自己留下的孤儿 / Clean up our own orphan:")
        print("        python3 app.py stop")
    print("  1) 换个端口：  NCGA_PORT=9000 python3 app.py")
    print(f"  2) 杀掉占用：  lsof -ti :{port} | xargs kill")
    print(f"     （强杀）：  lsof -ti :{port} | xargs kill -9")
    print(f"  3) 看看是谁：  lsof -i :{port}")
    print("=" * 72, flush=True)
    print()


# ---------------- lifecycle: PID file + signal handlers (Cycle 5 lessons L1+L2) ----------------


def _default_pid_file() -> Path:
    """Project-local PID file. Path is stable across runs so `app.py stop` finds it.

    `NCGA_PID_FILE` env var overrides — useful when running multiple instances
    (e.g. dev server on 8000 alongside an isolated preview on 8765) so they don't
    clobber each other's PID file.
    """
    override = os.environ.get("NCGA_PID_FILE", "").strip()
    if override:
        return Path(override)
    return BASE_DIR / ".ncga.pid"


def _write_pid_file(pid_file: Path) -> None:
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")


def _read_pid_file(pid_file: Path) -> int | None:
    if not pid_file.is_file():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _remove_pid_file(pid_file: Path) -> None:
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Could not remove pid file %s: %s", pid_file, exc)


def _pid_alive(pid: int) -> bool:
    """Is this PID alive *and not a zombie*?

    Cycle 5 lesson: `kill(pid, 0)` returns success for zombies (dead-but-unreaped children)
    on macOS/Linux. If pid is OUR child, reap it via waitpid(WNOHANG) first so we get
    truthful liveness. If pid is not our child, fall back to the signal-0 probe.
    """
    try:
        reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            # We just reaped a zombie that was our child — definitively dead.
            return False
    except ChildProcessError:
        pass  # not our child; signal-0 probe will be authoritative
    except OSError:
        pass  # EINVAL or similar; fall through

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but different user; still alive from our perspective.
        return True
    return True


def _port_in_use(host: str, port: int) -> bool:
    """Bind-based probe with SO_REUSEADDR: returns True iff a real listener holds the port.

    Cycle 17: SO_REUSEADDR added so a TIME_WAIT 4-tuple (left over from SIGKILL ~1s
    ago) is NOT misreported as "in use". Without it, `app.py stop` printed a false
    "port still occupied" right after a successful kill. With SO_REUSEADDR the
    semantics match what we actually want: "can a new listener bind here?".
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _wait_until_port_free(host: str, port: int, timeout: float = 5.0) -> bool:
    """Cycle 5 lesson L3: never trust kill() return code; assert the post-condition."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_in_use(host, port):
            return True
        time.sleep(0.2)
    return False


def stop_server(
    pid_file: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    timeout: float = 5.0,
) -> int:
    """Stop a previously-started instance via the PID file.

    Returns the resolved exit code: 0 = stopped cleanly, 1 = no PID file or already dead,
    2 = had to escalate to SIGKILL, 3 = could not stop.
    """
    import signal

    pid_file = pid_file or _default_pid_file()
    host = host or os.environ.get("NCGA_HOST", "127.0.0.1")
    port = port or int(os.environ.get("NCGA_PORT", "8000"))

    pid = _read_pid_file(pid_file)
    if pid is None:
        print(f"ℹ️  没找到 PID 文件 ({pid_file})，可能没跑。", flush=True)
        # Best-effort fallback: kill whoever holds the port if it's a python process
        holder = _find_port_holder(port)
        if holder and "Python" in holder:
            print(f"    但端口 {port} 被 {holder} 占着；试着干掉它。", flush=True)
            holder_pid = int(holder.split("PID ")[1].rstrip(")"))
            try:
                os.kill(holder_pid, signal.SIGTERM)
                if _wait_until_port_free(host, port, timeout):
                    print("✅ 已干掉端口占用者。", flush=True)
                    return 0
                os.kill(holder_pid, signal.SIGKILL)
                if _wait_until_port_free(host, port, timeout):
                    print("✅ 强杀成功。", flush=True)
                    return 2
            except (ProcessLookupError, PermissionError) as exc:
                print(f"❌ 干不掉：{exc}", flush=True)
                return 3
        return 1

    if not _pid_alive(pid):
        print(f"ℹ️  PID 文件里写的 {pid} 已经不存在了，清理 PID 文件。", flush=True)
        _remove_pid_file(pid_file)
        return 1

    print(f"📤 发 SIGTERM 给 PID {pid} …", flush=True)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pid_file(pid_file)
        return 1

    # L3: assert post-condition — wait until BOTH the PID is dead AND the port is free
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid) and not _port_in_use(host, port):
            print(f"✅ 已停止 (PID {pid})，端口 {port} 已释放。", flush=True)
            _remove_pid_file(pid_file)
            return 0
        time.sleep(0.2)

    print("⚠️  SIGTERM 后超时未停，升级到 SIGKILL …", flush=True)
    import contextlib

    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    if _wait_until_port_free(host, port, timeout):
        print(f"✅ 强杀完成，端口 {port} 释放。", flush=True)
        _remove_pid_file(pid_file)
        return 2
    print(f"❌ 强杀后端口 {port} 仍被占用。请手动 `lsof -i :{port}` 查看。", flush=True)
    return 3


def status_server(pid_file: Path | None = None, host: str | None = None, port: int | None = None) -> int:
    pid_file = pid_file or _default_pid_file()
    host = host or os.environ.get("NCGA_HOST", "127.0.0.1")
    port = port or int(os.environ.get("NCGA_PORT", "8000"))
    pid = _read_pid_file(pid_file)
    holder = _find_port_holder(port)
    print(f"PID file:   {pid_file}  ->  {pid if pid else '(none)'}")
    if pid:
        print(f"Process:    {'alive' if _pid_alive(pid) else 'dead'}")
    print(f"Port {port}: {'in use' if _port_in_use(host, port) else 'free'}")
    if holder:
        print(f"Held by:    {holder}")
    return 0


# ---------------- run_server with full lifecycle ----------------


def run_server(host: str | None = None, port: int | None = None) -> None:
    """Development server. Production should use waitress / gunicorn (see README)."""
    import signal
    import sys
    from wsgiref.simple_server import make_server

    load_dotenv()
    configure_logging(os.environ.get("NCGA_LOG_LEVEL", "INFO"))

    host = host or os.environ.get("NCGA_HOST", "127.0.0.1")
    port = port or int(os.environ.get("NCGA_PORT", "8000"))
    pid_file = _default_pid_file()

    logger.warning(
        "Starting wsgiref dev server on http://%s:%s — DO NOT USE IN PRODUCTION. "
        "Use waitress / gunicorn for real deployments.",
        host,
        port,
    )
    try:
        server = make_server(host, port, application)
    except OSError as exc:
        if exc.errno in (48, 98):  # EADDRINUSE on macOS / Linux
            _print_port_in_use_help(host, port, pid_file)
            sys.exit(2)
        raise

    # Install handlers BEFORE writing the PID file so a fast-arriving SIGTERM
    # doesn't fall through to the default action (instant kill, no cleanup).
    shutdown_event = {"requested": False}

    def graceful(signum, _frame):
        if shutdown_event["requested"]:
            return
        shutdown_event["requested"] = True
        signame = (
            "SIGINT"
            if signum == signal.SIGINT
            else "SIGTERM"
            if signum == signal.SIGTERM
            else f"signal {signum}"
        )
        print(f"\n📥 收到 {signame}，正在优雅关闭… / Graceful shutdown initiated.", flush=True)
        # Raising KeyboardInterrupt from the signal handler propagates through serve_forever's
        # selector and into our `finally` block. This avoids the threaded-shutdown deadlock
        # we hit on Python 3.14 where server.shutdown() could block longer than expected.
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, graceful)
    signal.signal(signal.SIGTERM, graceful)

    _write_pid_file(pid_file)
    logger.info("PID file written: %s (pid=%d)", pid_file, os.getpid())

    try:
        with server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass  # signal handler raised this; finally below handles cleanup
    finally:
        # CLEANUP — runs on every exit path (normal / signal / exception). Cycle 5 L4.
        _remove_pid_file(pid_file)
        print("👋 再见 / Bye.", flush=True)
        # Some environments (subprocess with detached stdout, certain Python 3.14 builds)
        # hang in interpreter shutdown after a signal-driven exit. Cleanup is done; force exit.
        if shutdown_event["requested"]:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)


def main() -> None:
    """Entry point with subcommands: start (default), stop, status."""
    import sys

    args = sys.argv[1:]
    if not args or args[0] in ("start", "run"):
        run_server()
    elif args[0] == "stop":
        sys.exit(stop_server())
    elif args[0] == "status":
        sys.exit(status_server())
    elif args[0] in ("-h", "--help", "help"):
        print(
            "usage: python3 app.py [start|stop|status]\n"
            "  start   (default) launch the dev server, write .ncga.pid\n"
            "  stop    SIGTERM the previous instance, escalate to SIGKILL if needed\n"
            "  status  show PID file, process liveness, and port state\n"
        )
        sys.exit(0)
    else:
        print(f"unknown command: {args[0]} (try: start | stop | status)")
        sys.exit(64)


if __name__ == "__main__":
    main()
