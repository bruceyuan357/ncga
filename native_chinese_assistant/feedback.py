"""Quality-feedback store (Cycle 9 — trimmed in Cycle 23).

Every rewrite the user rates via `rate_quality` produces a (variety, scenario, score)
data point. We accumulate these with Welford's online algorithm — numerically stable
streaming mean + variance, no need to retain individual samples — and expose the
per-(variety, scenario) aggregates to the `/api/quality-stats` dashboard.

History: this module once hosted a Reflexion-style self-improving prompt loop
(Generator → Critic → Refiner) that consumed these stats to auto-propose prompt
refinements. That Refiner was removed in Cycle 23; the store is now purely a rating
aggregator + dashboard feed.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Persist runs on every record() (the rate-quality hot path); keep the crypto import
# at module scope so any failure surfaces at import time, not at first persist.
from native_chinese_assistant.crypto import encrypt, resolve_key

logger = logging.getLogger("ncga.feedback")

# Reserved document key holding the operational-counters dict. Can't collide
# with a real bucket key — those are always "<variety>::<scenario>".
_COUNTERS_KEY = "__counters__"


@dataclass
class WelfordStats:
    """Streaming mean + variance per Welford's algorithm. No sample retention required."""

    n: int = 0
    mean: float = 0.0
    m2: float = 0.0  # sum of squared deviations from running mean
    min: float = math.inf
    max: float = -math.inf

    def update(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        delta2 = x - self.mean
        self.m2 += delta * delta2
        if x < self.min:
            self.min = x
        if x > self.max:
            self.max = x

    @property
    def variance(self) -> float:
        if self.n < 2:
            return 0.0
        return self.m2 / (self.n - 1)

    @property
    def stddev(self) -> float:
        return math.sqrt(self.variance)

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.n,
            "mean": round(self.mean, 3),
            "stddev": round(self.stddev, 3),
            "min": None if self.n == 0 else round(self.min, 1),
            "max": None if self.n == 0 else round(self.max, 1),
        }


@dataclass
class BucketState:
    """Per (variety, scenario) rating aggregate."""

    stats: WelfordStats = field(default_factory=WelfordStats)


class QualityStore:
    """Thread-safe rating-aggregate store, persisted to disk as JSON (AES-GCM at rest)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        # A plain Lock suffices: no method holds the lock while calling another
        # lock-taking method. (The reentrancy that once forced an RLock came from
        # stats_snapshot() calling ab_delta()/needs_reflection(); both were removed
        # with the Refiner in Cycle 23.)
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], BucketState] = {}
        self._counters: dict[str, int] = {}
        if self.path and self.path.is_file():
            self._load()

    # --- key helpers ---

    @staticmethod
    def _key(variety: str, scenario: str) -> tuple[str, str]:
        return (variety, scenario)

    @staticmethod
    def _key_str(variety: str, scenario: str) -> str:
        return f"{variety}::{scenario}"

    @staticmethod
    def _from_key_str(s: str) -> tuple[str, str]:
        v, _, sc = s.partition("::")
        return v, sc

    # --- mutators ---

    def record(self, variety: str, scenario: str, score: float) -> None:
        """Fold one quality score into the (variety, scenario) running stats."""
        with self._lock:
            key = self._key(variety, scenario)
            bucket = self._buckets.setdefault(key, BucketState())
            bucket.stats.update(float(score))
            self._persist_unlocked()

    def increment(self, counter: str, amount: int = 1) -> None:
        """Bump a named integer counter (e.g. rewrite request/degraded counts).

        Counters live in the same persisted document under the reserved
        `__counters__` key — they are operational telemetry, not ratings, so
        they must never land in a (variety, scenario) bucket and skew means.
        """
        with self._lock:
            self._counters[counter] = self._counters.get(counter, 0) + amount
            self._persist_unlocked()

    # --- accessors ---

    def get_bucket(self, variety: str, scenario: str) -> BucketState | None:
        with self._lock:
            return self._buckets.get(self._key(variety, scenario))

    def stats_snapshot(self) -> list[dict[str, Any]]:
        """Used by the /api/quality-stats endpoint."""
        out = []
        with self._lock:
            for (variety, scenario), bucket in self._buckets.items():
                out.append(
                    {
                        "variety": variety,
                        "scenario": scenario,
                        "stats": bucket.stats.as_dict(),
                    }
                )
        return out

    def counters_snapshot(self) -> dict[str, int]:
        """Operational counters (requests, degradations) for the dashboard."""
        with self._lock:
            return dict(self._counters)

    # --- persistence ---

    def _persist_unlocked(self) -> None:
        if not self.path:
            return
        # Cycle 13: AES-GCM at rest if a key is available.
        key = resolve_key()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            data: dict[str, Any] = {
                self._key_str(v, s): {
                    "stats": {
                        "n": b.stats.n,
                        "mean": b.stats.mean,
                        "m2": b.stats.m2,
                        "min": None if b.stats.min == math.inf else b.stats.min,
                        "max": None if b.stats.max == -math.inf else b.stats.max,
                    },
                }
                for (v, s), b in self._buckets.items()
            }
            if self._counters:
                data[_COUNTERS_KEY] = dict(self._counters)
            # Cycle 21 self-audit #10: drop indent when encrypting (the blob is opaque
            # anyway; indent ~doubles bytes-on-disk + bytes-fed-to-AES-GCM for nothing).
            # Keep indent for the plaintext path so the file stays greppable.
            if key is not None:
                payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                tmp.write_bytes(encrypt(payload, key))
            else:
                payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                tmp.write_bytes(payload)
            os.chmod(tmp, 0o600)  # restrict perms regardless
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("Could not persist QualityStore: %s", exc)

    def _quarantine(self, reason: str) -> None:
        """Cycle 17: rename a load-failed file aside instead of letting the next
        persist() overwrite it. We learned this the hard way — silent return-on-
        decrypt-fail meant subsequent record() calls clobbered legitimate data
        encrypted under a now-rotated/mismatched key. Quarantining preserves the
        bytes so the user can recover (or at least diagnose) what happened.

        Only for files that are genuinely unparseable (truncated blob, bad JSON).
        Key problems (missing key, KeyMismatchError) must NOT come here — they
        raise in _load() instead, leaving the healthy file in place.
        """
        try:
            from datetime import datetime

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_suffix(self.path.suffix + f".corrupt-{stamp}")
            self.path.rename(backup)
            logger.error(
                "QualityStore at %s could not be loaded (%s); quarantined to %s. "
                "Inspect / restore manually; a fresh empty store will be used until then.",
                self.path,
                reason,
                backup,
            )
        except OSError as exc:
            logger.error(
                "QualityStore at %s could not be loaded (%s) AND quarantine failed (%s); "
                "refusing to start with an empty store that would clobber the file. "
                "Move or delete the file by hand.",
                self.path,
                reason,
                exc,
            )
            raise RuntimeError(f"QualityStore unreadable and quarantine failed at {self.path}") from exc

    def _load(self) -> None:
        from native_chinese_assistant.crypto import KeyMismatchError, decrypt, is_encrypted, resolve_key

        try:
            blob = self.path.read_bytes()
        except OSError as exc:
            logger.warning("Could not read QualityStore %s: %s", self.path, exc)
            return
        if is_encrypted(blob):
            key = resolve_key()
            if key is None:
                # No key — don't quarantine (user might just need to set the env var).
                # Refuse to start so we don't overwrite the encrypted file with empty plaintext.
                raise RuntimeError(
                    f"QualityStore at {self.path} is encrypted but NCGA_DATA_KEY is not set. "
                    f"Set the key env var or remove/rename the file to start fresh."
                )
            try:
                blob = decrypt(blob, key)
            except KeyMismatchError as exc:
                # Wrong key — same treatment as key-missing above: refuse, leave the
                # file untouched. Quarantining here is how healthy stores got benched
                # 16 times between 2026-04 and 2026-06 (resolve_key() fell back to the
                # user keyfile whenever .env wasn't loaded). The file is almost
                # certainly fine under the right key; renaming it aside just hides it.
                raise RuntimeError(
                    f"QualityStore at {self.path} could not be authenticated with the "
                    f"resolved data key ({exc}). Likely a wrong or unloaded NCGA_DATA_KEY "
                    f"(e.g. .env not sourced), or a stale user keyfile. File left "
                    f"untouched — load the correct key, or remove/rename the file to "
                    f"start fresh."
                ) from exc
            except Exception as exc:  # noqa: BLE001
                # Structurally broken blob (truncated, bad framing) — genuinely corrupt.
                self._quarantine(f"decrypt failed: {exc}")
                return
        try:
            data = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._quarantine(f"parse failed: {exc}")
            return
        for key_str, payload in data.items():
            if key_str == _COUNTERS_KEY:
                if isinstance(payload, dict):
                    self._counters = {str(k): int(v) for k, v in payload.items()}
                continue
            v, s = self._from_key_str(key_str)
            stats_d = payload.get("stats", {})
            # Legacy stores may carry override/draft/sample fields from the old Refiner;
            # we read only `stats` now and silently drop the rest.
            self._buckets[(v, s)] = BucketState(
                stats=WelfordStats(
                    n=stats_d.get("n", 0),
                    mean=stats_d.get("mean", 0.0),
                    m2=stats_d.get("m2", 0.0),
                    min=stats_d.get("min") if stats_d.get("min") is not None else math.inf,
                    max=stats_d.get("max") if stats_d.get("max") is not None else -math.inf,
                ),
            )
