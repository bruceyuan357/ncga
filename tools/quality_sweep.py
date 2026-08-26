#!/usr/bin/env python3
"""LLM-judge quality sweep (2026-08).

Rewrites a fixed set of probe sentences into every dialect, judges each output
with the pro tier (deepseek-v4-pro by default), and folds the scores into the
quality store under scenario "judge_sweep" — giving /quality a trend line for
output quality instead of waiting for users to click stars.

Usage:
    python3 tools/quality_sweep.py                  # full sweep: 20 sentences × 10 dialects
    python3 tools/quality_sweep.py --sentences 5    # cheaper partial sweep
    python3 tools/quality_sweep.py --varieties beijing_mandarin,cantonese_written
    python3 tools/quality_sweep.py --dry-run        # print scores, write nothing

Cost: sentences × varieties rewrites (flash, thinking off) + the same number
of judge calls (pro tier). A full 20×10 sweep is 400 calls — a few cents.
Requires DEEPSEEK_API_KEY in .env.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from native_chinese_assistant.feedback import QualityStore  # noqa: E402
from native_chinese_assistant.presets import PRESET_METADATA, VarietyPreset  # noqa: E402
from native_chinese_assistant.rewrite import (  # noqa: E402
    ChatCompletionsClient,
    RewriteError,
    RewriteService,
    load_dotenv,
    load_llm_config,
)

# Fixed probe set: registers a dialect rewriter actually meets — casual chat,
# service talk, workplace, family, tech, and one tricky classical-flavored line.
PROBE_SENTENCES = [
    "今天天气真好,我们去公园散步吧。",
    "这个东西多少钱?能不能便宜一点?",
    "我明天要开会,可能来不了了。",
    "你吃饭了吗?没吃的话一起吧。",
    "这孩子真聪明,一学就会。",
    "路上堵车,我大概晚到二十分钟。",
    "麻烦帮我把这个文件打印两份。",
    "最近工作特别忙,天天加班。",
    "你的提议很好,但是预算不够。",
    "别忘了给妈妈打个电话。",
    "这家店的菜特别地道,下次还来。",
    "我的电脑又死机了,得重装系统。",
    "请问地铁站怎么走?远不远?",
    "这部电影一般般,没什么意思。",
    "你把话说清楚,到底是什么意思?",
    "周末有空的话,来我家吃饭。",
    "身体是革命的本钱,别太累了。",
    "这个事情先放一放,以后再说。",
    "他这个人靠谱,交给他的事放心。",
    "当局者迷,旁观者清。",
]

SCENARIO_TAG = "judge_sweep"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sentences", type=int, default=len(PROBE_SENTENCES), help="first N probe sentences")
    parser.add_argument("--varieties", type=str, default="", help="comma-separated variety subset")
    parser.add_argument("--judge-model", type=str, default="deepseek-v4-pro", help="model used for judging")
    parser.add_argument("--parallel", type=int, default=3, help="concurrent LLM calls (be polite)")
    parser.add_argument("--store", type=str, default="", help="quality store path override")
    parser.add_argument("--dry-run", action="store_true", help="print scores, write nothing to the store")
    args = parser.parse_args()

    load_dotenv()
    config = load_llm_config()
    if config is None:
        print("error: no LLM configured (DEEPSEEK_API_KEY missing)", file=sys.stderr)
        return 2
    client = ChatCompletionsClient(config)
    service = RewriteService(client=client)

    sentences = PROBE_SENTENCES[: max(1, args.sentences)]
    if args.varieties:
        wanted = {v.strip() for v in args.varieties.split(",") if v.strip()}
        varieties = [v for v in VarietyPreset if v.value in wanted]
        if not varieties:
            print(f"error: no known varieties in {args.varieties!r}", file=sys.stderr)
            return 2
    else:
        varieties = list(VarietyPreset)

    store: QualityStore | None = None
    if not args.dry_run:
        if args.store:
            store = QualityStore(path=Path(args.store))
        else:
            # Same default resolution as the web app: XDG data dir.
            import os

            xdg = os.environ.get("XDG_DATA_HOME", "").strip()
            base = Path(xdg) / "ncga" if xdg else Path.home() / ".local" / "share" / "ncga"
            base.mkdir(parents=True, exist_ok=True)
            store = QualityStore(path=base / "quality.json")

    jobs = [(sentence, variety) for sentence in sentences for variety in varieties]
    print(f"sweep: {len(sentences)} sentences × {len(varieties)} varieties = {len(jobs)} cells")

    def run_one(sentence: str, variety: VarietyPreset) -> tuple[str, float | None]:
        try:
            result = service.rewrite(sentence, variety)
            if result.degraded:
                print(f"  warn: {variety.value} degraded to heuristic for {sentence[:12]}…")
                return variety.value, None
            judged = client.rate_quality(result.rewritten_text, variety, model=args.judge_model)
            return variety.value, float(judged["score"])
        except (RewriteError, ValueError) as exc:
            print(f"  warn: {variety.value} failed for {sentence[:12]}…: {exc}")
            return variety.value, None

    scores: dict[str, list[float]] = {v.value: [] for v in varieties}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        for variety_value, score in pool.map(lambda j: run_one(*j), jobs):
            done += 1
            if score is not None:
                scores[variety_value].append(score)
                if store is not None:
                    store.record(variety_value, SCENARIO_TAG, score)
            if done % 20 == 0:
                print(f"  …{done}/{len(jobs)}")

    print()
    print(f"{'variety':<42} {'n':>4} {'mean':>6} {'min':>5} {'max':>5}")
    weakest: tuple[str, float] | None = None
    for variety in varieties:
        values = scores[variety.value]
        label = PRESET_METADATA[variety].label
        if not values:
            print(f"{label:<42} {'0':>4}     —     —     —")
            continue
        mean = statistics.fmean(values)
        print(f"{label:<42} {len(values):>4} {mean:>6.2f} {min(values):>5.1f} {max(values):>5.1f}")
        if weakest is None or mean < weakest[1]:
            weakest = (label, mean)
    if weakest is not None:
        print(f"\nweakest dialect this sweep: {weakest[0]} ({weakest[1]:.2f}/5) — next corpus hour goes there")
    print("dry-run: nothing written" if store is None else f"recorded under scenario {SCENARIO_TAG!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
