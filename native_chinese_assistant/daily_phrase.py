"""Daily phrase module (Cycle 18, Function 2).

Picks one wisdom phrase per calendar day deterministically from a curated pool,
then asks the LLM to rewrite it into all 10 varieties. Result is cached for 24h
in ~/.local/share/ncga/phrase-cache.json so only the first call of the day pays
the LLM cost (~10 rewrites).

Cycle 18 v2 (per user A1 + A2):
  * Image: returns ALL 12 of China's most representative landmarks (cultural +
    scenic balance). Frontend rotates them as a fast slideshow, so each day's
    phrase is staged against the country's full visual sweep, not one place.
    All URLs are reused from PRESET_METADATA — already CSP-whitelisted Wikimedia,
    no new image source / no new CSP entry / no licensing review.
  * Pool refresh: the curated _SEED_POOL is "generation 0" (first 30 days).
    After day 30, the LLM is asked to author a fresh batch of 30 phrases —
    cached as generation 1, 2, … in ~/.local/share/ncga/phrase-pool-genN.json.
    Total LLM cost: 1 refresh call every 30 days + 10 rewrites every day.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from native_chinese_assistant.presets import (
    Scenario,
    VarietyPreset,
)

if TYPE_CHECKING:
    from native_chinese_assistant.rewrite import RewriteService

logger = logging.getLogger("ncga.daily_phrase")

# 24 hours in seconds. We refresh by calendar date, not by elapsed time, so this
# is just a sanity bound for cache invalidation if the date check somehow misses.
CACHE_TTL_SECONDS = 26 * 60 * 60


@dataclass(frozen=True)
class WisdomSeed:
    """One curated wisdom phrase. `original` is the canonical Putonghua form;
    `meaning` is a one-line modern reading the user can grok at a glance."""

    original: str
    meaning: str


# Curated pool of well-known 谚语 / 古训 / 俗语.
# Keep these short, evergreen, and emotionally neutral — they show up at the top
# of the workbench every day, so anything with topical weight (politics, sex,
# religion) is excluded by intent.
_SEED_POOL: list[WisdomSeed] = [
    WisdomSeed("活到老，学到老。", "学习是一辈子的事，没有截止日期。"),
    WisdomSeed("一寸光阴一寸金，寸金难买寸光阴。", "时间最贵，钱再多也买不回来。"),
    WisdomSeed("路遥知马力，日久见人心。", "时间会让人和事的本相浮出来。"),
    WisdomSeed("吃一堑，长一智。", "栽过的跟头，会变成下一次的眼力。"),
    WisdomSeed("众人拾柴火焰高。", "一群人一起干，比一个人能撑起更大的火。"),
    WisdomSeed("家和万事兴。", "家里气氛对了，外面的事才办得下去。"),
    WisdomSeed("远亲不如近邻。", "真正能搭把手的，往往是身边那个人。"),
    WisdomSeed("一日之计在于晨。", "一天的方向，往往在早上就被定下来。"),
    WisdomSeed("三人行，必有我师焉。", "再普通的人身上，也总有你能学的东西。"),
    WisdomSeed("欲速则不达。", "急着抄近路，反而走得更远。"),
    WisdomSeed("良药苦口利于病，忠言逆耳利于行。", "听着不舒服的话，往往最值得听完。"),
    WisdomSeed("有志者，事竟成。", "心里真想做的事，时间会把它磨成形。"),
    WisdomSeed("书山有路勤为径，学海无涯苦作舟。", "通往知识的路只有一条：肯花时间。"),
    WisdomSeed("不积跬步，无以至千里。", "大事，是被一小步一小步堆出来的。"),
    WisdomSeed("熟能生巧。", "做得多了，手自然就有数了。"),
    WisdomSeed("失败是成功之母。", "走错的那几步，恰好垫高了下一步。"),
    WisdomSeed("机不可失，时不再来。", "对的时机经过你身边，可能只那一次。"),
    WisdomSeed("满招损，谦受益。", "把自己看小一点，世界才装得进来。"),
    WisdomSeed("百闻不如一见。", "听人家讲一百遍，不如自己去看一眼。"),
    WisdomSeed("人无远虑，必有近忧。", "今天不抬头看路，明天就会被路绊倒。"),
    WisdomSeed("己所不欲，勿施于人。", "你自己不爱受的，别去给别人。"),
    WisdomSeed("大道至简。", "真正的好东西，往往简单得让人意外。"),
    WisdomSeed("水滴石穿。", "再硬的事，被时间和耐心反复磨，都会让步。"),
    WisdomSeed("近朱者赤，近墨者黑。", "你常待在谁身边，慢慢就会变成那种人。"),
    WisdomSeed("当局者迷，旁观者清。", "事情绕不开的时候，先抽身看一眼。"),
    WisdomSeed("塞翁失马，焉知非福。", "眼前的不顺，可能是后来好运的前奏。"),
    WisdomSeed("千里之行，始于足下。", "再远的路，第一脚永远在脚边。"),
    WisdomSeed("有备无患。", "把功课提前做了，慌乱就少一半。"),
    WisdomSeed("少壮不努力，老大徒伤悲。", "年轻时省下的力气，老的时候要加倍还。"),
    WisdomSeed("一分耕耘，一分收获。", "你给生活几分认真，生活就还你几分。"),
]


# --- A2: 12 most-representative Chinese landmarks (cultural + scenic balance) ---
# All URLs are already vetted in PRESET_METADATA → already CSP-whitelisted, no new
# upstream needed. Captions are tuned to draw the cultural / scenic / regional axis.
TOP_12_LANDMARKS: list[tuple[str, str]] = [
    (
        "https://commons.wikimedia.org/wiki/Special:FilePath/The_Great_Wall_of_China_at_Jinshanling-edit.jpg?width=2400",
        "万里长城 · 北方雄关",
    ),
    (
        "https://commons.wikimedia.org/wiki/Special:FilePath/Hall_of_Supreme_Harmony_%2820241127120000%29.jpg?width=2400",
        "故宫太和殿 · 帝王礼制",
    ),
    (
        "https://commons.wikimedia.org/wiki/Special:FilePath/Temple_of_Heaven_20160323_01.jpg?width=2400",
        "天坛 · 天人对话",
    ),
    (
        "https://commons.wikimedia.org/wiki/Special:FilePath/Longevity_Hill_of_the_Summer_Palace.jpg?width=2400",
        "颐和园 · 江南皇苑",
    ),
    (
        "https://commons.wikimedia.org/wiki/Special:FilePath/Huangshan_fengjing.jpg?width=2400",
        "黄山 · 山岳之冠",
    ),
    (
        "https://commons.wikimedia.org/wiki/Special:FilePath/1_nuorilang_jiuzhaigou_2023.jpg?width=2400",
        "九寨沟 · 高原秘境",
    ),
    (
        "https://commons.wikimedia.org/wiki/Special:FilePath/Leshan_Giant_Buddha_%281%29.jpg?width=2400",
        "乐山大佛 · 千年佛缘",
    ),
    (
        "https://commons.wikimedia.org/wiki/Special:FilePath/Wuyi_Mountains_Sea_of_clouds_4.jpg?width=2400",
        "武夷山 · 东南胜景",
    ),
    (
        "https://commons.wikimedia.org/wiki/Special:FilePath/Promenade_du_Bund.jpg?width=2400",
        "上海外滩 · 海上风华",
    ),
    (
        "https://commons.wikimedia.org/wiki/Special:FilePath/Qutang_Gorge_on_Changjiang.jpg?width=2400",
        "三峡瞿塘峡 · 大江东去",
    ),
    (
        "https://commons.wikimedia.org/wiki/Special:FilePath/%E9%BC%93%E6%B5%AA%E5%B1%BF_-_panoramio.jpg?width=2400",
        "鼓浪屿 · 海岛人文",
    ),
    (
        "https://commons.wikimedia.org/wiki/Special:FilePath/West_facade_of_St._Sophia_Cathedral%2C_Harbin_%2820230721150450%29.jpg?width=2400",
        "哈尔滨圣索菲亚教堂 · 北国异韵",
    ),
]

# --- A1: pool versioning ---
# Generation 0 = curated _SEED_POOL above (free, on-disk forever).
# Generation 1, 2, … = LLM-authored 30-phrase batches; cached on disk; one LLM
# refresh call per 30 days. The reference epoch is fixed so generations are
# deterministic across machines / clocks.
POOL_EPOCH = date(2026, 5, 1)
POOL_LENGTH = 30


def _cache_path() -> Path:
    """Match QualityStore's location convention so all per-user data lives together."""
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) / "ncga" if xdg else Path.home() / ".local" / "share" / "ncga"
    base.mkdir(parents=True, exist_ok=True)
    return base / "phrase-cache.json"


def _pool_path(generation: int) -> Path:
    """Where generation N's pool lives. Generation 0 is in-code (_SEED_POOL)."""
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) / "ncga" if xdg else Path.home() / ".local" / "share" / "ncga"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"phrase-pool-gen{generation}.json"


def _days_since_epoch(today: date) -> int:
    return (today - POOL_EPOCH).days


def _todays_pool_generation(today: date) -> int:
    """Generation 0 = first 30 days. Generation N = days [N*30, (N+1)*30)."""
    days = _days_since_epoch(today)
    if days < 0:
        # Clock before epoch — fall back to generation 0 (use seed pool).
        return 0
    return days // POOL_LENGTH


def _todays_index_in_pool(today: date) -> int:
    """Deterministic index 0..29 within the active pool generation.
    Hash spreads consecutive days across the pool to avoid e.g. "Mon=item-0 always"."""
    days = max(_days_since_epoch(today), 0)
    rotation = days % POOL_LENGTH
    # Mix in the generation so a refreshed pool doesn't replay the same order
    gen = _todays_pool_generation(today)
    h = int(hashlib.sha256(f"{gen}:{rotation}".encode()).hexdigest(), 16)
    return h % POOL_LENGTH


# --- A1: LLM-authored fresh pool ---
_REFRESH_SYSTEM_PROMPT = (
    "你是中文谚语 / 古训选编人。请挑出 30 条积极向上、不涉及政治、宗教、性别敏感"
    "议题的中文谚语或处世格言，可以来自古训、谚语、俗语、白话哲思。\n\n"
    "硬性规则：\n"
    "1. 每条 original ≤ 18 字。\n"
    "2. 每条 meaning ≤ 30 字（用现代白话给出解读，不要文言）。\n"
    "3. 30 条之间互相不重复、立意不雷同。\n"
    "4. 严格 JSON 输出，必须是数组，不要 markdown，不要前导语：\n"
    '   [{"original": "...", "meaning": "..."}, ... 共 30 条 ... ]\n'
    "5. 排除已经过于熟悉到失去新意的几条："
    "「活到老学到老」「有志者事竟成」「失败是成功之母」（请避开这三条）。\n"
)


def _generate_fresh_pool(service: RewriteService) -> list[WisdomSeed]:
    """Ask the LLM for 30 fresh phrases as a JSON array. Falls back to _SEED_POOL on
    any failure (parse error, timeout, etc.) — refresh failure must NEVER take the
    daily-phrase feature offline. The seed pool is always a valid backup."""
    # getattr (not service._client) so a service object that simply has no
    # client attribute falls back to the seed pool too — the docstring promises
    # refresh failure NEVER takes the feature offline, and an AttributeError
    # here would break that promise (it did: a cold cache + a client-less
    # service raised instead of degrading gracefully).
    client = getattr(service, "_client", None)
    if client is None:
        logger.warning("phrase-pool refresh skipped: no LLM client")
        return list(_SEED_POOL)
    try:
        content = client.general_chat(
            [
                {"role": "system", "content": _REFRESH_SYSTEM_PROMPT},
                {"role": "user", "content": "请给我下一批 30 条。"},
            ],
            max_tokens=3000,  # 30 phrases × ~80 chars each + JSON overhead
            temperature=0.7,
            thinking="disabled",  # phrase generation needs no CoT; thinking ate
            # the whole 3000 budget once (2026-08) → empty content → seed fallback
        )
        if not content or not content.strip():
            raise ValueError("empty content")
        # Cycle 21 self-audit #6: was `json.loads(content)`. DeepSeek-V4 sometimes
        # wraps responses in ```json ... ``` fences even with
        # response_format=json_object. Plain json.loads chokes; we silently fall
        # back to the static seed pool and operator never sees the refresh failed.
        # _parse_llm_json strips fences + tolerates preamble.
        from native_chinese_assistant.rewrite import _parse_llm_json

        # _parse_llm_json returns a dict. Our schema expects either a JSON
        # array directly OR a {"phrases":[...]} wrapper. Handle both.
        try:
            parsed: Any = _parse_llm_json(content)
        except Exception:
            # If even the tolerant parser fails, try raw — content might be
            # an array, which _parse_llm_json rejects as "non-object JSON".
            parsed = json.loads(content)
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    parsed = v
                    break
        if not isinstance(parsed, list):
            raise ValueError(f"expected JSON array, got {type(parsed).__name__}")
        out: list[WisdomSeed] = []
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            original = str(entry.get("original", "")).strip()[:40]
            meaning = str(entry.get("meaning", "")).strip()[:60]
            if original and meaning:
                out.append(WisdomSeed(original=original, meaning=meaning))
        if len(out) < 10:
            raise ValueError(f"only {len(out)} valid entries; refusing low-quality pool")
        # Pad to POOL_LENGTH from _SEED_POOL if the LLM returned fewer than 30.
        while len(out) < POOL_LENGTH:
            out.append(_SEED_POOL[len(out) % len(_SEED_POOL)])
        return out[:POOL_LENGTH]
    except Exception as exc:  # noqa: BLE001
        logger.warning("phrase-pool refresh failed (%s); falling back to seed pool", exc)
        return list(_SEED_POOL)


def _get_or_generate_pool(service: RewriteService, generation: int) -> list[WisdomSeed]:
    """Generation 0 → in-code _SEED_POOL. Generation N → load from disk if cached,
    else ask LLM and persist."""
    if generation == 0:
        return list(_SEED_POOL)
    path = _pool_path(generation)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(cached, list) and len(cached) >= POOL_LENGTH:
            return [WisdomSeed(**x) for x in cached[:POOL_LENGTH]]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    pool = _generate_fresh_pool(service)
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps([{"original": s.original, "meaning": s.meaning} for s in pool], ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Could not persist phrase pool gen %d: %s", generation, exc)
    return pool


def _load_cache(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Could not persist phrase cache: %s", exc)


# Module-level lock so concurrent first-of-the-day calls don't both spawn 10 LLM
# requests. The first wins; subsequent waiters read the cache.
_lock = threading.Lock()


def get_phrase_of_the_day(service: RewriteService) -> dict[str, Any]:
    """Returns the cached payload for today, generating it on first call.

    Payload shape (Cycle 18 v2):
        {
          "date": "YYYY-MM-DD",
          "pool_generation": int,          # 0 = curated seed pool, ≥1 = LLM-authored
          "original_phrase": "...",
          "meaning": "...",
          "translations": { variety_value: rewritten_text or "__error__:reason" },
          "images": [{"url": "...", "caption": "..."}, ... 12 entries ...],
        }

    Side effects on the first call of a new generation (every 30 days):
      * One LLM call to author the next 30-phrase pool, persisted to disk.
      * Subsequent days in that generation read from the persisted pool.
    """
    today = date.today()
    today_iso = today.isoformat()
    path = _cache_path()

    cached = _load_cache(path)
    # Stale-fallback: wall-clock TTL is the safety net in case the system date jumps
    # backwards (rare but possible on travel / VM restore).
    if (
        cached
        and cached.get("date") == today_iso
        and cached.get("translations")
        and cached.get("images")  # require new shape; old cache is stale
        and time.time() - cached.get("_generated_at", 0) < CACHE_TTL_SECONDS
    ):
        return _strip_internal(cached)

    with _lock:
        # Re-check after acquiring lock — another thread may have just finished.
        cached = _load_cache(path)
        if cached and cached.get("date") == today_iso and cached.get("translations") and cached.get("images"):
            return _strip_internal(cached)

        generation = _todays_pool_generation(today)
        pool = _get_or_generate_pool(service, generation)
        idx = _todays_index_in_pool(today)
        seed = pool[idx]

        translations: dict[str, str] = {}
        # Cycle 18: rewrite into all 10 varieties using the existing service.
        # Use friends_casual scenario by default — wisdom phrases land best as
        # something a friend would say. Errors per-variety are recorded inline
        # so a single failure doesn't kill the whole landing card.
        # 2026-08: capped parallel (3, matching the batch endpoint's proven
        # default). Was sequential: 10 × ~2s LLM calls = 13.5s+ first-hit
        # latency every morning. Capped at 3 to stay polite to the provider
        # and inside the per-IP rate buckets.
        from concurrent.futures import ThreadPoolExecutor

        def _rewrite_one(variety: VarietyPreset) -> tuple[str, str]:
            try:
                result = service.rewrite(
                    seed.original,
                    variety,
                    scenario=Scenario.FRIENDS_CASUAL,
                )
                return variety.value, result.rewritten_text
            except Exception as exc:  # noqa: BLE001
                logger.warning("daily-phrase rewrite failed for %s: %s", variety.value, exc)
                return variety.value, f"__error__:{type(exc).__name__}"

        with ThreadPoolExecutor(max_workers=3) as executor:
            for value, text in executor.map(_rewrite_one, VarietyPreset):
                translations[value] = text

        # With the heuristic fallback gone, a keyless or flaky server yields
        # "__error__" for every variety. Persisting that as today's card kept
        # the landing page broken for CACHE_TTL_SECONDS even after the operator
        # added a key. Serve it, but don't cache it — the next visitor retries.
        any_success = any(not str(t).startswith("__error__") for t in translations.values())
        images = [{"url": url, "caption": caption} for url, caption in TOP_12_LANDMARKS]
        payload = {
            "date": today_iso,
            "pool_generation": generation,
            "original_phrase": seed.original,
            "meaning": seed.meaning,
            "translations": translations,
            "images": images,
            "_generated_at": time.time(),
        }
        if any_success:
            _save_cache(path, payload)
        else:
            logger.warning("daily-phrase: all %d rewrites failed; not caching", len(translations))
        return _strip_internal(payload)


def _strip_internal(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop fields that start with `_` (internal bookkeeping like _generated_at)."""
    return {k: v for k, v in payload.items() if not k.startswith("_")}
