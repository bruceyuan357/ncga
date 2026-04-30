"""Quality-feedback store + self-improving prompt system (Cycle 9).

The point: every rewrite that gets rated by `rate_quality` produces a (variety, scenario,
score, original, rewritten, reason) record. We accumulate these and use distributional
statistics to decide WHEN a (variety, scenario) prompt is underperforming. Then we ask
the LLM itself to reflect on its high vs low scoring examples and propose a refined
prompt addendum to use going forward.

Reflexion-style loop (per Shinn et al., 2023):
  Generator (rewrite) → Critic (rate_quality) → Refiner (this module's meta_refine)
  with persistent episodic memory (this module's QualityStore).

Welford's online algorithm gives us numerically stable streaming mean+variance with
no need to keep all samples in memory for the stat — we keep recent samples only as
prompt context for the Refiner.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("ncga.feedback")

DEFAULT_TRIGGER_MIN_COUNT = 8  # don't reflect until we have at least N samples
DEFAULT_TRIGGER_THRESHOLD = 3.5  # mean score below this triggers meta-reflection
DEFAULT_MAX_RECENT = 50  # keep last N samples per (variety, scenario) for context
DEFAULT_HI_LO_PER_REFLECT = 4  # show top 4 + bottom 4 examples to the meta-LLM


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
class Sample:
    """One rated rewrite. Recent samples are kept as Refiner context."""

    score: float
    original: str
    rewritten: str
    reason: str
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BucketState:
    """Per (variety, scenario) state.

    Lifecycle of a refined addendum (Cycle 10 — human-in-the-loop):
        meta_refine() → sets `draft_addendum` (NOT activated)
        user reviews in UI → calls activate_override() → copies draft into
            `override_addendum` (active) + snapshots `samples_pre_activation`
        user rejects → reject_draft() clears draft only
        user later regrets → clear_override() removes active and stats since
            activation are kept for record
    """

    stats: WelfordStats = field(default_factory=WelfordStats)
    samples: list[Sample] = field(default_factory=list)
    # Currently-active override (the only thing build_system_prompt looks at).
    override_addendum: str | None = None
    override_reason: str | None = None
    override_generated_at: float | None = None
    override_baseline_mean: float | None = None
    # Pending draft proposed by Refiner; awaits human approval.
    draft_addendum: str | None = None
    draft_reason: str | None = None
    draft_generated_at: float | None = None
    draft_baseline_mean: float | None = None
    # Snapshot of stats taken at activation time, so we can show A/B delta.
    activation_baseline_count: int = 0
    activation_baseline_mean: float | None = None
    activation_baseline_at: float | None = None  # epoch
    last_reflected_at: float = 0.0  # epoch — throttles auto-reflection


class QualityStore:
    """Thread-safe quality-feedback store, persisted to disk as JSON."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_recent: int = DEFAULT_MAX_RECENT,
        trigger_min_count: int = DEFAULT_TRIGGER_MIN_COUNT,
        trigger_threshold: float = DEFAULT_TRIGGER_THRESHOLD,
        reflect_cooldown_s: float = 300.0,
    ) -> None:
        self.path = path
        self.max_recent = max_recent
        self.trigger_min_count = trigger_min_count
        self.trigger_threshold = trigger_threshold
        self.reflect_cooldown_s = reflect_cooldown_s
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], BucketState] = {}
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

    def record(
        self,
        variety: str,
        scenario: str,
        score: float,
        original: str,
        rewritten: str,
        reason: str = "",
    ) -> None:
        with self._lock:
            key = self._key(variety, scenario)
            bucket = self._buckets.setdefault(key, BucketState())
            bucket.stats.update(score)
            bucket.samples.append(
                Sample(
                    score=float(score),
                    original=original[:300],
                    rewritten=rewritten[:600],
                    reason=reason[:120],
                )
            )
            if len(bucket.samples) > self.max_recent:
                bucket.samples = bucket.samples[-self.max_recent :]
            self._persist_unlocked()

    def set_draft(
        self,
        variety: str,
        scenario: str,
        addendum: str,
        reason: str,
        baseline_mean: float | None = None,
    ) -> None:
        """Cycle 10: meta_refine writes the proposed addendum here, NOT directly to override.
        Activation requires explicit activate_override() call (human approval)."""
        with self._lock:
            key = self._key(variety, scenario)
            bucket = self._buckets.setdefault(key, BucketState())
            bucket.draft_addendum = addendum
            bucket.draft_reason = reason
            bucket.draft_generated_at = time.time()
            bucket.draft_baseline_mean = baseline_mean
            bucket.last_reflected_at = time.time()  # cooldown still applies — don't re-refine
            self._persist_unlocked()

    def activate_override(
        self,
        variety: str,
        scenario: str,
        addendum: str | None = None,  # None = use current draft as-is
        reason: str | None = None,
    ) -> bool:
        """Promote draft (or user-edited addendum) to active override.
        Snapshots current stats so we can compute A/B delta over future samples."""
        with self._lock:
            bucket = self._buckets.get(self._key(variety, scenario))
            if not bucket:
                return False
            if addendum is None:
                if not bucket.draft_addendum:
                    return False
                addendum = bucket.draft_addendum
                if reason is None:
                    reason = bucket.draft_reason
            bucket.override_addendum = addendum
            bucket.override_reason = reason or "(manually activated)"
            bucket.override_generated_at = time.time()
            bucket.override_baseline_mean = bucket.draft_baseline_mean or bucket.stats.mean
            # Snapshot baseline for A/B
            bucket.activation_baseline_count = bucket.stats.n
            bucket.activation_baseline_mean = bucket.stats.mean
            bucket.activation_baseline_at = time.time()
            # Clear draft now that it's promoted
            bucket.draft_addendum = None
            bucket.draft_reason = None
            bucket.draft_generated_at = None
            bucket.draft_baseline_mean = None
            self._persist_unlocked()
            return True

    def reject_draft(self, variety: str, scenario: str) -> bool:
        with self._lock:
            bucket = self._buckets.get(self._key(variety, scenario))
            if not bucket or not bucket.draft_addendum:
                return False
            bucket.draft_addendum = None
            bucket.draft_reason = None
            bucket.draft_generated_at = None
            bucket.draft_baseline_mean = None
            self._persist_unlocked()
            return True

    def clear_override(self, variety: str, scenario: str) -> bool:
        with self._lock:
            bucket = self._buckets.get(self._key(variety, scenario))
            if not bucket or not bucket.override_addendum:
                return False
            bucket.override_addendum = None
            bucket.override_reason = None
            bucket.override_generated_at = None
            bucket.override_baseline_mean = None
            bucket.activation_baseline_count = 0
            bucket.activation_baseline_mean = None
            bucket.activation_baseline_at = None
            self._persist_unlocked()
            return True

    # Cycle 10 backwards-compat: existing API `set_override` becomes a thin shim
    # that goes draft → activate atomically. Used only by tests; production now
    # always goes through set_draft + activate_override.
    def set_override(
        self,
        variety: str,
        scenario: str,
        addendum: str,
        reason: str,
        baseline_mean: float | None = None,
    ) -> None:
        self.set_draft(variety, scenario, addendum, reason, baseline_mean)
        self.activate_override(variety, scenario)

    # --- accessors ---

    def get_override_addendum(self, variety: str, scenario: str) -> str | None:
        bucket = self._buckets.get(self._key(variety, scenario))
        return bucket.override_addendum if bucket else None

    def get_bucket(self, variety: str, scenario: str) -> BucketState | None:
        return self._buckets.get(self._key(variety, scenario))

    def needs_reflection(self, variety: str, scenario: str) -> bool:
        """True if (variety, scenario) bucket has enough data AND mean is below threshold
        AND cooldown has elapsed since last reflection."""
        bucket = self._buckets.get(self._key(variety, scenario))
        if not bucket:
            return False
        if bucket.stats.n < self.trigger_min_count:
            return False
        if bucket.stats.mean >= self.trigger_threshold:
            return False
        return time.time() - bucket.last_reflected_at >= self.reflect_cooldown_s

    def hi_lo_examples(
        self,
        variety: str,
        scenario: str,
        n_each: int = DEFAULT_HI_LO_PER_REFLECT,
    ) -> tuple[list[Sample], list[Sample]]:
        """Return (hi_score, lo_score) sample lists for Refiner context."""
        bucket = self._buckets.get(self._key(variety, scenario))
        if not bucket:
            return [], []
        sorted_samples = sorted(bucket.samples, key=lambda s: s.score)
        lo = sorted_samples[:n_each]
        hi = sorted_samples[-n_each:][::-1]
        return hi, lo

    def ab_delta(self, variety: str, scenario: str) -> dict[str, Any] | None:
        """If override is active and we've collected new samples since activation,
        compute the mean of pre-activation samples vs post-activation samples.

        Returns None if no override at all. For legacy stores that pre-date the
        activation_baseline_* fields, falls back to override_baseline_mean which
        Cycle 9 wrote at refine-time (close enough — same mean we'd have snapshotted).
        Returns {post_count: 0, ...} if override is fresh / no new samples yet.
        """
        bucket = self._buckets.get(self._key(variety, scenario))
        if not bucket or not bucket.override_addendum:
            return None
        # Prefer the explicit activation snapshot; fall back for legacy data.
        if bucket.activation_baseline_count and bucket.activation_baseline_count > 0:
            baseline_count = bucket.activation_baseline_count
            baseline = bucket.activation_baseline_mean or 0.0
        elif bucket.override_baseline_mean is not None:
            # Legacy: at Cycle 9 we recorded only the mean; we don't know the count.
            # Treat ALL existing samples in the bucket as post-activation since we
            # can't tell which were pre vs post. This makes A/B less meaningful for
            # legacy data — UI should disclose with a "(legacy)" tag.
            baseline_count = 0
            baseline = bucket.override_baseline_mean
        else:
            return None  # truly no baseline info
        post_count = bucket.stats.n - baseline_count
        if post_count <= 0:
            return {
                "post_count": 0,
                "baseline_count": baseline_count,
                "baseline_mean": round(baseline, 2),
                "post_mean": None,
                "delta": None,
                "legacy": baseline_count == 0,
            }
        recent = bucket.samples[-post_count:] if post_count <= len(bucket.samples) else bucket.samples
        post_mean = sum(s.score for s in recent) / len(recent) if recent else 0.0
        return {
            "post_count": len(recent),
            "baseline_count": baseline_count,
            "baseline_mean": round(baseline, 2),
            "post_mean": round(post_mean, 2),
            "delta": round(post_mean - baseline, 2),
            "legacy": baseline_count == 0,
        }

    def stats_snapshot(self) -> list[dict[str, Any]]:
        """Used by /api/quality-stats endpoint."""
        out = []
        with self._lock:
            for (variety, scenario), bucket in self._buckets.items():
                out.append(
                    {
                        "variety": variety,
                        "scenario": scenario,
                        "stats": bucket.stats.as_dict(),
                        "has_override": bool(bucket.override_addendum),
                        "override_at": bucket.override_generated_at,
                        "override_reason": bucket.override_reason,
                        "override_baseline_mean": bucket.override_baseline_mean,
                        "has_draft": bool(bucket.draft_addendum),
                        "draft_addendum": bucket.draft_addendum,
                        "draft_reason": bucket.draft_reason,
                        "draft_at": bucket.draft_generated_at,
                        "draft_baseline_mean": bucket.draft_baseline_mean,
                        "ab": self.ab_delta(variety, scenario),
                        "last_reflected_at": bucket.last_reflected_at,
                        "needs_reflection": self.needs_reflection(variety, scenario),
                    }
                )
        return out

    # --- persistence ---

    def _persist_unlocked(self) -> None:
        if not self.path:
            return
        # Cycle 13: AES-GCM at rest if a key is available
        from native_chinese_assistant.crypto import encrypt, resolve_key

        key = resolve_key()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            data = {
                self._key_str(v, s): {
                    "stats": {
                        "n": b.stats.n,
                        "mean": b.stats.mean,
                        "m2": b.stats.m2,
                        "min": None if b.stats.min == math.inf else b.stats.min,
                        "max": None if b.stats.max == -math.inf else b.stats.max,
                    },
                    "samples": [s.as_dict() for s in b.samples],
                    "override_addendum": b.override_addendum,
                    "override_reason": b.override_reason,
                    "override_generated_at": b.override_generated_at,
                    "override_baseline_mean": b.override_baseline_mean,
                    "draft_addendum": b.draft_addendum,
                    "draft_reason": b.draft_reason,
                    "draft_generated_at": b.draft_generated_at,
                    "draft_baseline_mean": b.draft_baseline_mean,
                    "activation_baseline_count": b.activation_baseline_count,
                    "activation_baseline_mean": b.activation_baseline_mean,
                    "activation_baseline_at": b.activation_baseline_at,
                    "last_reflected_at": b.last_reflected_at,
                }
                for (v, s), b in self._buckets.items()
            }
            payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            if key is not None:
                tmp.write_bytes(encrypt(payload, key))
            else:
                tmp.write_bytes(payload)
            os.chmod(tmp, 0o600)  # restrict perms regardless
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("Could not persist QualityStore: %s", exc)

    def _load(self) -> None:
        from native_chinese_assistant.crypto import decrypt, is_encrypted, resolve_key

        try:
            blob = self.path.read_bytes()
        except OSError as exc:
            logger.warning("Could not read QualityStore %s: %s", self.path, exc)
            return
        if is_encrypted(blob):
            key = resolve_key()
            if key is None:
                logger.warning(
                    "QualityStore at %s is encrypted but no key available; refusing to load",
                    self.path,
                )
                return
            try:
                blob = decrypt(blob, key)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Could not decrypt QualityStore at %s: %s — file may be tampered or wrong key",
                    self.path,
                    exc,
                )
                return
        try:
            data = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("Could not parse QualityStore from %s: %s", self.path, exc)
            return
        for key_str, payload in data.items():
            v, s = self._from_key_str(key_str)
            stats_d = payload.get("stats", {})
            bucket = BucketState(
                stats=WelfordStats(
                    n=stats_d.get("n", 0),
                    mean=stats_d.get("mean", 0.0),
                    m2=stats_d.get("m2", 0.0),
                    min=stats_d.get("min") if stats_d.get("min") is not None else math.inf,
                    max=stats_d.get("max") if stats_d.get("max") is not None else -math.inf,
                ),
                samples=[Sample(**sd) for sd in payload.get("samples", [])],
                override_addendum=payload.get("override_addendum"),
                override_reason=payload.get("override_reason"),
                override_generated_at=payload.get("override_generated_at"),
                override_baseline_mean=payload.get("override_baseline_mean"),
                # Cycle 10 fields — fall back to None for legacy stores
                draft_addendum=payload.get("draft_addendum"),
                draft_reason=payload.get("draft_reason"),
                draft_generated_at=payload.get("draft_generated_at"),
                draft_baseline_mean=payload.get("draft_baseline_mean"),
                activation_baseline_count=payload.get("activation_baseline_count", 0),
                activation_baseline_mean=payload.get("activation_baseline_mean"),
                activation_baseline_at=payload.get("activation_baseline_at"),
                last_reflected_at=payload.get("last_reflected_at", 0.0),
            )
            self._buckets[(v, s)] = bucket


# --- Refiner: build the meta-prompt that asks the LLM to refine the prompt addendum ---


def build_refiner_user_prompt(
    variety_label: str,
    scenario_label: str,
    current_addendum: str,
    hi_samples: list[Sample],
    lo_samples: list[Sample],
) -> str:
    def fmt_samples(label: str, samples: list[Sample]) -> str:
        if not samples:
            return f"（{label}：无样本）"
        lines = [f"【{label}】"]
        for i, s in enumerate(samples, 1):
            lines.append(
                f"  例 {i}（评分 {s.score:.1f}/5，评语：{s.reason or '—'}）\n"
                f"    原文：{s.original}\n"
                f"    改写：{s.rewritten}"
            )
        return "\n".join(lines)

    hi = fmt_samples("高分例", hi_samples)
    lo = fmt_samples("低分例", lo_samples)
    return (
        f"任务：你是 NCGA 系统的 prompt 调优师。\n"
        f"目标方言 / 场景：{variety_label} / {scenario_label}\n\n"
        f"当前【场景指引】片段：\n{current_addendum}\n\n"
        f"以下是同一方言+场景下的高分输出与低分输出。请你对比它们的差别，"
        f"找出哪些特征被低分输出忽略了、哪些特征高分输出做对了。\n\n"
        f"{hi}\n\n{lo}\n\n"
        f"请你重写这个【场景指引】片段，把高分例里成功的模式显式写进规则，"
        f"把低分例里反复出现的失误显式禁止掉。\n\n"
        f"严格 JSON 输出（不要 markdown）：\n"
        '  {"new_addendum": "...", "diff_summary": "一句话说明改了什么"}\n\n'
        f"硬性要求：\n"
        f"1. new_addendum 要保留原有的【场景】结构（开头还是「【场景】xxx」），但内容更具体；\n"
        f"2. 用 ≤ 220 个汉字，写得像可执行规则不是抽象建议；\n"
        f"3. 至少给 1-2 个具体词汇/句式的「应该」和 1 个具体的「不要」；\n"
        f"4. diff_summary ≤ 30 字，不要解释自己。"
    )


REFINER_SYSTEM_PROMPT = (
    "你是一位中文方言 prompt 调优专家。你的工作是看一批同一方言+场景下的高分/低分改写样本，"
    "诊断当前指引为什么不够好，重写指引让它更有效。"
    "你必须严格按 JSON 输出，不要任何解释、markdown 或前导语。"
)
